# Multi-page setup wizard for Ankisstant.
# 5 pages: Intro (provider) → Credentials → Model → Defaults → Test & card
# Entry point: open_welcome() — unchanged public API.

from __future__ import annotations

import json

from aqt import mw
from aqt.qt import (
    QButtonGroup, QComboBox, QDialog, QFrame, QGroupBox, QHBoxLayout,
    QLabel, QLineEdit, QPushButton, QRadioButton, QStackedWidget, Qt,
    QTextEdit, QVBoxLayout, QWidget,
)
from aqt.utils import showWarning, tooltip

from ..core import api as core_api
from ..core.config import PROVIDER_MODELS, family_for, load_config, save_config
from ..core import log


# ── provider catalogue ─────────────────────────────────────────────────────────
# (key, label, sublabel_html, link_text, link_url, detail_html)
_PROVIDERS = [
    ("cli",
     "Claude Pro / Max",
     "Already paying for Claude Pro or Max",
     None, None,
     "<b>Requires a Claude Pro or Max subscription</b> (~$20/mo) plus a one-time "
     "install of the Claude Code CLI (free software, takes a few minutes).<br><br>"
     "Your subscription covers all Ankisstant usage — no extra charges. "
     "All data stays on your machine and is never sent to any server."),

    ("manual",
     "Copy-paste from any free AI",
     "Zero setup — works with free ChatGPT, Gemini, or Claude.ai",
     None, None,
     "<b>Works with the free web versions of ChatGPT, Gemini, and Claude.ai.</b><br><br>"
     "Ankisstant assembles a ready-made prompt for you. You copy it into any AI chatbot, "
     "then paste the reply back. Nothing leaves your machine automatically.<br><br>"
     "Pre-built AI agents for Anki card creation are also listed below — "
     "open one and paste your source text directly."),

    ("anthropic",
     "Anthropic API key",
     "Want the best Claude quality, okay paying a few cents per use",
     "Get a key at console.anthropic.com", "https://console.anthropic.com",
     "<b>Direct API access to Claude — not the same as a Claude.ai subscription.</b><br><br>"
     "You need an API key from Anthropic's developer console with a small prepaid credit "
     "balance. A typical Ankisstant session costs a few cents. "
     "Data is sent to Anthropic's servers.<br><br>"
     "<b style='color:#b85c00;'>Note:</b> New keys can take up to 15 minutes to activate. "
     "If the connection test fails at first, wait a few minutes and try again."),

    ("gemini",
     "Google Gemini API key",
     "Want a free API — no credit card required",
     "Get a free key at aistudio.google.com/apikey",
     "https://aistudio.google.com/apikey",
     "<b>Google AI Studio gives free Gemini API access — separate from your Google account.</b><br><br>"
     "Sign up at Google AI Studio and get a key in under a minute, no credit card needed.<br><br>"
     "<b style='color:#b85c00;'>Privacy:</b> Google's free tier may use your prompts to "
     "improve their AI models. Use a paid tier or choose a different provider if this "
     "is a concern.<br><br>"
     "<b style='color:#b85c00;'>Note:</b> New keys can take up to 15 minutes to activate."),

    ("openai",
     "OpenAI API key",
     "Already have an OpenAI developer account",
     "Get a key at platform.openai.com/api-keys",
     "https://platform.openai.com/api-keys",
     "<b>OpenAI's developer API — separate billing from ChatGPT Plus.</b><br><br>"
     "You need an API key from OpenAI's platform with a small prepaid credit balance. "
     "A ChatGPT Plus subscription does not include API access. "
     "Data is sent to OpenAI's servers.<br><br>"
     "<b style='color:#b85c00;'>Note:</b> New keys can take up to 15 minutes to activate."),

    ("ollama",
     "Local AI with Ollama",
     "Privacy-first — runs entirely on your computer (needs 16 GB+ RAM)",
     "Download Ollama at ollama.com",
     "https://ollama.com",
     "<b>Run AI entirely on your own computer — nothing leaves your machine.</b><br><br>"
     "Requires installing Ollama (free, open source) and downloading a model such as "
     "llama3.1, mistral, or phi3 (several GB each).<br><br>"
     "<b>Hardware:</b> 16 GB RAM is the practical minimum; 32 GB recommended for "
     "larger models. Apple Silicon Macs and recent NVIDIA GPUs work well. "
     "Older or low-RAM machines may be too slow to be usable."),
]

# Per-family model metadata: id → (friendly name, tier description)
_MODEL_META: dict[str, tuple[str, str]] = {
    "claude-opus-4-7":           ("Opus 4.7",         "Most capable — complex multi-step reasoning"),
    "claude-sonnet-4-6":         ("Sonnet 4.6",        "Recommended — excellent quality, reasonable speed"),
    "claude-haiku-4-5-20251001": ("Haiku 4.5",         "Fast & cheap — great for searches and quick tasks"),
    "gemini-2.5-pro":            ("Gemini 2.5 Pro",    "Highest quality — slower, uses more quota"),
    "gemini-2.5-flash":          ("Gemini 2.5 Flash",  "Recommended — fast and capable"),
    "gemini-2.0-flash":          ("Gemini 2.0 Flash",  "Free-tier workhorse, very fast"),
    "gpt-4o":                    ("GPT-4o",            "Most capable OpenAI model"),
    "gpt-4o-mini":               ("GPT-4o mini",       "Recommended — cheap and capable"),
    "gpt-4.1-mini":              ("GPT-4.1 mini",      "Fast and economical"),
}

# Default model index (into PROVIDER_MODELS list) per family
_DEFAULT_IDX = {"anthropic": 1, "gemini": 2, "openai": 1, "ollama": 0}

# Page indices
_P_INTRO    = 0
_P_CREDS    = 1
_P_MODEL    = 2
_P_DEFAULTS = 3
_P_TEST     = 4

# Pre-built AI agents for the manual copy-paste workflow.
# Fill in real URLs once the GPTs are published; None = show as "coming soon".
_PREMADE_GPTS: list[tuple[str, str | None]] = [
    ("Cloze Card Generator", None),   # TODO: add URL when GPT is ready
    ("Basic Card Generator", None),   # TODO: add URL when GPT is ready
]

# Prompt sent to generate the test card
_TEST_CARD_PROMPT = (
    "Generate exactly ONE Anki cloze deletion card about the mechanism "
    "of action of aspirin.\n\n"
    "Reply with ONLY this JSON object — no markdown, no code fence, "
    "no explanation:\n"
    '{"front":"...{{c1::...}}...","extra":"..."}\n\n'
    "Rules:\n"
    "- front must contain at least one {{c1::text}} cloze\n"
    "- keep front to one sentence (the key fact as a fill-in-the-blank)\n"
    "- extra: 1-2 sentences of clinical context\n"
    "- medically accurate\n"
)

_DEFAULT_AUDIT_PREFIX = "Ankisstant::AI"


def _hsep() -> QFrame:
    line = QFrame()
    line.setFrameShape(QFrame.Shape.HLine)
    line.setFrameShadow(QFrame.Shadow.Sunken)
    return line


def _card(content: QWidget, bg: str = "rgba(255,255,255,0.06)",
          border: str = "rgba(128,128,128,0.25)") -> QFrame:
    """Wrap a widget in a rounded card-style frame."""
    frame = QFrame()
    frame.setStyleSheet(
        f"QFrame {{ background:{bg}; border:1px solid {border}; "
        f"border-radius:8px; }}"
    )
    lay = QVBoxLayout(frame)
    lay.setContentsMargins(14, 12, 14, 12)
    lay.addWidget(content)
    return frame


def _h2(text: str) -> QLabel:
    lbl = QLabel(text)
    lbl.setTextFormat(Qt.TextFormat.RichText)
    f = lbl.font()
    f.setPointSize(f.pointSize() + 3)
    f.setBold(True)
    lbl.setFont(f)
    return lbl


def _muted(html: str, wrap: bool = True) -> QLabel:
    lbl = QLabel(html)
    lbl.setTextFormat(Qt.TextFormat.RichText)
    lbl.setStyleSheet("color: gray;")
    if wrap:
        lbl.setWordWrap(True)
    return lbl


def _rich(html: str, wrap: bool = True) -> QLabel:
    """RichText label without forced gray — inline HTML styles apply."""
    lbl = QLabel(html)
    lbl.setTextFormat(Qt.TextFormat.RichText)
    if wrap:
        lbl.setWordWrap(True)
    return lbl


# ── pages ─────────────────────────────────────────────────────────────────────

class _PageIntro(QWidget):
    """Page 0: explain what Ankisstant is, pick provider."""

    def __init__(self, wizard: SetupWizard, parent=None):
        super().__init__(parent)
        self._wizard = wizard
        root = QVBoxLayout(self)
        root.setContentsMargins(24, 16, 24, 12)
        root.setSpacing(8)

        root.addWidget(_h2("Welcome to Ankisstant"))

        tools_lbl = _muted(
            "Ankisstant adds four AI tools to Anki: "
            "<b>AI QBank</b> (log and card-ify missed questions), "
            "<b>AI Browse</b> (natural-language search), "
            "<b>Analyse Knowledge Gaps</b>, and "
            "<b>AI Create</b> (cards from text, PDFs, URLs)."
        )
        root.addWidget(tools_lbl)

        root.addWidget(_hsep())

        q_lbl = QLabel("<b>How will you use AI with Ankisstant?</b>")
        q_lbl.setTextFormat(Qt.TextFormat.RichText)
        root.addWidget(q_lbl)

        self._grp = QButtonGroup(self)
        for i, (key, label, sublabel, *_rest) in enumerate(_PROVIDERS):
            btn = QRadioButton()
            self._grp.addButton(btn, i)
            row_widget = QWidget()
            row_lay = QHBoxLayout(row_widget)
            row_lay.setContentsMargins(0, 0, 0, 0)
            row_lay.setSpacing(8)
            row_lay.addWidget(btn)
            inner = QVBoxLayout()
            inner.setSpacing(1)
            name_lbl = QLabel(f"<b>{label}</b>")
            name_lbl.setTextFormat(Qt.TextFormat.RichText)
            sub_lbl = QLabel(sublabel)
            sub_lbl.setStyleSheet("color: gray; font-size: 11px;")
            inner.addWidget(name_lbl)
            inner.addWidget(sub_lbl)
            row_lay.addLayout(inner, 1)
            # Click anywhere on the row selects the radio.
            name_lbl.mousePressEvent = lambda _e, b=btn: b.setChecked(True)
            sub_lbl.mousePressEvent  = lambda _e, b=btn: b.setChecked(True)
            root.addWidget(row_widget)

        # Pre-select based on existing config.
        existing = (wizard.cfg.get("provider") or "auto").lower()
        pre = {
            "auto": 0, "cli": 0,
            "manual": 1,
            "anthropic": 2, "gemini": 3, "openai": 4,
            "ollama": 5,
        }
        btn = self._grp.button(pre.get(existing, 0))
        if btn:
            btn.setChecked(True)

        root.addStretch(1)

    def selected_provider(self) -> str:
        idx = self._grp.checkedId()
        if idx < 0:
            return "cli"
        return _PROVIDERS[idx][0]

    def validate(self) -> bool:
        return True


class _PageCredentials(QWidget):
    """Page 1: provider-specific credentials.  Refreshed on each show."""

    def __init__(self, wizard: SetupWizard, parent=None):
        super().__init__(parent)
        self._wizard = wizard
        self._cli_found: bool = False

        self._root = QVBoxLayout(self)
        self._root.setContentsMargins(24, 20, 24, 16)
        self._root.setSpacing(12)
        self._container = QWidget()
        self._root.addWidget(self._container)
        self._root.addStretch(1)

    def refresh(self) -> None:
        """Rebuild content for the current provider."""
        old = self._container
        self._container = QWidget()
        self._root.insertWidget(0, self._container)
        old.deleteLater()

        provider = self._wizard.current_provider
        lay = QVBoxLayout(self._container)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(12)

        link_text, link_url, detail = next(
            (link_text, link_url, detail)
            for k, _l, _sl, link_text, link_url, detail in _PROVIDERS
            if k == provider
        )

        if provider == "cli":
            lay.addWidget(_h2("Connect Claude Code CLI"))
            lay.addWidget(_rich(detail))
            lay.addWidget(_hsep())

            self._cli_status = QLabel("Checking…")
            self._cli_status.setStyleSheet("font-size: 12px;")
            lay.addWidget(self._cli_status)

            detect_row = QHBoxLayout()
            self._detect_btn = QPushButton("Detect Claude CLI")
            self._detect_btn.clicked.connect(self._detect_cli)
            detect_row.addWidget(self._detect_btn)
            detect_row.addStretch(1)
            lay.addLayout(detect_row)

            lay.addWidget(_muted(
                "<small>On macOS, the GUI app doesn't inherit your shell PATH so "
                "detection may fail. If so, run <code>which claude</code> in Terminal "
                "and paste the result below.</small>"
            ))

            path_lbl = QLabel("CLI path (leave blank to auto-detect):")
            self._cli_path = QLineEdit(self._wizard.cfg.get("claude_cli_path", "") or "")
            self._cli_path.setPlaceholderText("/usr/local/bin/claude  (auto-detect if blank)")
            self._cli_path.setMinimumWidth(420)
            lay.addWidget(path_lbl)
            lay.addWidget(self._cli_path)

            self._detect_cli(silent=True)

        elif provider == "manual":
            lay.addWidget(_h2("Copy-paste workflow"))
            lay.addWidget(_rich(detail))
            lay.addWidget(_hsep())

            info = QLabel(
                "No credentials needed here.<br><br>"
                "When you use any Ankisstant tool, it will assemble a ready-made "
                "prompt for you. Copy it into <b>ChatGPT / Gemini / "
                "Claude.ai</b> (the free web versions work fine), then paste the "
                "reply back.<br><br>"
                "<b>Click Next to continue.</b>"
            )
            info.setTextFormat(Qt.TextFormat.RichText)
            info.setWordWrap(True)
            info.setStyleSheet(
                "background: rgba(80,160,255,0.10); border: 1px solid "
                "rgba(80,160,255,0.4); border-radius: 8px; padding: 14px;"
            )
            lay.addWidget(info)

            # Pre-built GPT agents section
            lay.addWidget(_hsep())
            agents_title = QLabel("<b>Or use a pre-built AI agent (optional):</b>")
            agents_title.setTextFormat(Qt.TextFormat.RichText)
            lay.addWidget(agents_title)
            lay.addWidget(_muted(
                "These AI agents are pre-configured for Anki card creation — "
                "open the link, paste your source text, and get cards back directly."
            ))

            ready = [t for t in _PREMADE_GPTS if t[1]]
            pending = [t for t in _PREMADE_GPTS if not t[1]]

            for name, url in ready:
                lbl = QLabel(f'&nbsp;&nbsp;· <a href="{url}">{name}</a>')
                lbl.setTextFormat(Qt.TextFormat.RichText)
                lbl.setOpenExternalLinks(True)
                lay.addWidget(lbl)

            if pending:
                names = ", ".join(n for n, _ in pending)
                coming = QLabel(
                    f"&nbsp;&nbsp;· <span style='color:gray;'>{names} — coming soon</span>"
                )
                coming.setTextFormat(Qt.TextFormat.RichText)
                lay.addWidget(coming)

        elif provider == "ollama":
            lay.addWidget(_h2("Local AI with Ollama"))
            lay.addWidget(_rich(detail))
            if link_text and link_url:
                link_lbl = QLabel(f'<a href="{link_url}">{link_text}</a>')
                link_lbl.setTextFormat(Qt.TextFormat.RichText)
                link_lbl.setOpenExternalLinks(True)
                link_lbl.setStyleSheet("color: #2563eb;")
                lay.addWidget(link_lbl)
            lay.addWidget(_hsep())

            url_lbl = QLabel("Ollama server URL:")
            self._ollama_url = QLineEdit(
                self._wizard.cfg.get("ollama_base_url", "") or "http://localhost:11434"
            )
            self._ollama_url.setPlaceholderText("http://localhost:11434")
            self._ollama_url.setMinimumWidth(420)
            lay.addWidget(url_lbl)
            lay.addWidget(self._ollama_url)

            lay.addWidget(_muted(
                "<small>Start Ollama with <code>ollama serve</code>, then pull a model "
                "with e.g. <code>ollama pull llama3.1</code>. "
                "The model name you enter on the next page must match what you downloaded.</small>"
            ))

        else:
            # API key providers: anthropic / gemini / openai
            titles = {
                "anthropic": "Anthropic API key",
                "gemini":    "Google Gemini API key",
                "openai":    "OpenAI API key",
            }
            lay.addWidget(_h2(titles.get(provider, "API key")))
            lay.addWidget(_rich(detail))
            if link_text and link_url:
                link_lbl = QLabel(f'<a href="{link_url}">{link_text} ↗</a>')
                link_lbl.setTextFormat(Qt.TextFormat.RichText)
                link_lbl.setOpenExternalLinks(True)
                link_lbl.setStyleSheet("color: #2563eb;")
                lay.addWidget(link_lbl)
            lay.addWidget(_hsep())

            placeholders = {
                "anthropic": "sk-ant-…",
                "gemini":    "AIza…",
                "openai":    "sk-…",
            }
            key_lbl = QLabel("Paste your API key:")
            self._key_input = QLineEdit()
            self._key_input.setPlaceholderText(placeholders.get(provider, "key…"))
            self._key_input.setEchoMode(QLineEdit.EchoMode.Password)
            self._key_input.setMinimumWidth(460)
            # Pre-fill from existing config
            existing_keys = {
                "anthropic": self._wizard.cfg.get("anthropic_api_key", "") or "",
                "gemini":    self._wizard.cfg.get("gemini_api_key", "") or "",
                "openai":    self._wizard.cfg.get("openai_api_key", "") or "",
            }
            self._key_input.setText(existing_keys.get(provider, ""))
            lay.addWidget(key_lbl)
            key_row = QHBoxLayout()
            key_row.addWidget(self._key_input, 1)
            show_btn = QPushButton("Show")
            show_btn.setCheckable(True)
            show_btn.setFixedWidth(52)
            show_btn.toggled.connect(
                lambda on: self._key_input.setEchoMode(
                    QLineEdit.EchoMode.Normal if on else QLineEdit.EchoMode.Password
                )
            )
            key_row.addWidget(show_btn)
            lay.addLayout(key_row)

    def _detect_cli(self, *_args, silent: bool = False) -> None:
        path = core_api.detect_cli_path(
            self._wizard.cfg.get("claude_cli_path", "") or ""
        )
        self._cli_found = bool(path)
        if path:
            self._cli_status.setText(f"✓ Found: {path}")
            self._cli_status.setStyleSheet("color: #1c8000; font-size: 12px;")
        else:
            self._cli_status.setText(
                "Not found. Install Claude Code from docs.claude.com/claude-code, "
                "or paste the path below."
            )
            self._cli_status.setStyleSheet("color: gray; font-size: 12px;")
            if not silent:
                tooltip("Claude Code CLI not found on PATH or known locations.")

    def get_cli_path(self) -> str:
        if hasattr(self, "_cli_path"):
            return self._cli_path.text().strip()
        return ""

    def get_api_key(self) -> str:
        if hasattr(self, "_key_input"):
            return self._key_input.text().strip()
        return ""

    def get_ollama_url(self) -> str:
        if hasattr(self, "_ollama_url"):
            return self._ollama_url.text().strip()
        return ""

    def validate(self) -> bool:
        provider = self._wizard.current_provider
        if provider in ("manual", "cli", "ollama"):
            return True
        key = self.get_api_key()
        if not key:
            showWarning(
                "Paste your API key to continue, or go Back and choose a "
                "different option."
            )
            return False
        return True


class _PageModel(QWidget):
    """Page 2: pick a model for the chosen provider family."""

    def __init__(self, wizard: SetupWizard, parent=None):
        super().__init__(parent)
        self._wizard = wizard
        self._root = QVBoxLayout(self)
        self._root.setContentsMargins(24, 20, 24, 16)
        self._root.setSpacing(12)
        self._container = QWidget()
        self._root.addWidget(self._container)
        self._root.addStretch(1)

    def refresh(self) -> None:
        old = self._container
        self._container = QWidget()
        self._root.insertWidget(0, self._container)
        old.deleteLater()

        provider = self._wizard.current_provider
        family = family_for(provider)
        models = PROVIDER_MODELS.get(family, [])

        lay = QVBoxLayout(self._container)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(12)

        lay.addWidget(_h2("Choose a model"))
        lay.addWidget(_muted(
            "Pick the model Ankisstant uses by default. You can change this per "
            "tool in Settings later."
        ))
        lay.addWidget(_hsep())

        self._grp = QButtonGroup(self)
        current = self._wizard.cfg.get("model_defaults", {}).get(family, "")
        default_idx = _DEFAULT_IDX.get(family, 0)

        if models:
            for i, model_id in enumerate(models):
                friendly, tier = _MODEL_META.get(model_id, (model_id, ""))
                rb = QRadioButton()
                self._grp.addButton(rb, i)
                row_w = QWidget()
                rlay = QHBoxLayout(row_w)
                rlay.setContentsMargins(0, 2, 0, 2)
                rlay.setSpacing(8)
                rlay.addWidget(rb)
                inner = QVBoxLayout()
                inner.setSpacing(1)
                name_lbl = QLabel(
                    f"<b>{friendly}</b>  "
                    f"<span style='color:gray;font-size:10px;'>{model_id}</span>"
                )
                name_lbl.setTextFormat(Qt.TextFormat.RichText)
                tier_lbl = QLabel(tier)
                tier_lbl.setStyleSheet("color: gray; font-size: 11px;")
                inner.addWidget(name_lbl)
                inner.addWidget(tier_lbl)
                rlay.addLayout(inner, 1)
                name_lbl.mousePressEvent = lambda _e, b=rb: b.setChecked(True)
                tier_lbl.mousePressEvent = lambda _e, b=rb: b.setChecked(True)
                lay.addWidget(row_w)

                if current and model_id == current:
                    rb.setChecked(True)
                elif not current and i == default_idx:
                    rb.setChecked(True)
        else:
            # Ollama or unknown family: no preset list — prompt for custom name
            lay.addWidget(_muted(
                "Enter the name of the Ollama model you have downloaded, "
                "e.g. <code>llama3.1</code>, <code>mistral</code>, <code>phi3</code>."
            ))

        # Custom model ID row
        lay.addWidget(_hsep())
        custom_row = QHBoxLayout()
        custom_row.addWidget(_muted(
            "<small>Or type a custom model ID:</small>" if models
            else "<small>Model name:</small>",
            wrap=False
        ))
        self._custom = QLineEdit()
        self._custom.setPlaceholderText(
            "e.g. claude-opus-4-7" if models else "e.g. llama3.1"
        )
        self._custom.setMinimumWidth(280)
        self._custom.setFixedHeight(28)
        if not models and current:
            self._custom.setText(current)
        custom_row.addWidget(self._custom, 1)
        lay.addLayout(custom_row)

    def selected_model(self) -> str:
        custom = self._custom.text().strip() if hasattr(self, "_custom") else ""
        if custom:
            return custom
        if not hasattr(self, "_grp"):
            return ""
        idx = self._grp.checkedId()
        provider = self._wizard.current_provider
        family = family_for(provider)
        models = PROVIDER_MODELS.get(family, [])
        if 0 <= idx < len(models):
            return models[idx]
        return ""

    def validate(self) -> bool:
        return True


class _PageDefaults(QWidget):
    """Page 3: default deck and notetype for card creation."""

    def __init__(self, wizard: SetupWizard, parent=None):
        super().__init__(parent)
        self._wizard = wizard
        root = QVBoxLayout(self)
        root.setContentsMargins(24, 20, 24, 16)
        root.setSpacing(12)

        root.addWidget(_h2("Where should new cards go?"))
        root.addWidget(_muted(
            "These are the defaults for AI Create. You can override them any time "
            "you generate cards, and change them later in Settings."
        ))
        root.addWidget(_hsep())

        # Deck
        deck_lbl = QLabel("Default deck:")
        self._deck = QComboBox()
        self._deck.setEditable(True)
        self._deck.setMinimumWidth(420)
        self._populate_decks()
        root.addWidget(deck_lbl)
        root.addWidget(self._deck)

        root.addSpacing(8)

        # Notetype
        nt_lbl = QLabel("Default card type (notetype):")
        self._notetype = QComboBox()
        self._notetype.setEditable(False)
        self._notetype.setMinimumWidth(420)
        self._populate_notetypes()
        root.addWidget(nt_lbl)
        root.addWidget(self._notetype)

        self._nt_warn = QLabel("")
        self._nt_warn.setStyleSheet("color: #b85c00; font-size: 11px;")
        self._nt_warn.setWordWrap(True)
        root.addWidget(self._nt_warn)
        self._notetype.currentTextChanged.connect(self._check_notetype)
        self._check_notetype(self._notetype.currentText())

        root.addStretch(1)

    def _populate_decks(self):
        self._deck.clear()
        if mw.col is None:
            return
        try:
            names = sorted(d.name for d in mw.col.decks.all_names_and_ids())
        except Exception as e:
            log.warn(f"wizard: failed to list decks: {e}")
            return
        self._deck.addItems(names)
        current = (self._wizard.cfg.get("tools", {})
                   .get("card_creator", {}).get("default_deck", ""))
        if current:
            idx = self._deck.findText(current)
            if idx >= 0:
                self._deck.setCurrentIndex(idx)
            else:
                self._deck.setEditText(current)
        else:
            for guess in ("Default", "default"):
                idx = self._deck.findText(guess)
                if idx >= 0:
                    self._deck.setCurrentIndex(idx)
                    return

    def _populate_notetypes(self):
        self._notetype.clear()
        if mw.col is None:
            return
        try:
            self._nt_names = sorted(nt["name"] for nt in mw.col.models.all())
        except Exception as e:
            log.warn(f"wizard: failed to list notetypes: {e}")
            return
        self._notetype.addItems(self._nt_names)
        current = (self._wizard.cfg.get("tools", {})
                   .get("card_creator", {}).get("default_notetype", ""))
        if current:
            idx = self._notetype.findText(current)
            if idx >= 0:
                self._notetype.setCurrentIndex(idx)
                return
        # Prefer first cloze-type notetype.
        for name in getattr(self, "_nt_names", []):
            if self._is_cloze(name):
                idx = self._notetype.findText(name)
                if idx >= 0:
                    self._notetype.setCurrentIndex(idx)
                    return

    def _is_cloze(self, name: str) -> bool:
        try:
            nt = mw.col.models.by_name(name)
            return nt is not None and int(nt.get("type", 0)) == 1
        except Exception:
            return False

    def _check_notetype(self, name: str):
        if not name or self._is_cloze(name):
            self._nt_warn.setText("")
        else:
            self._nt_warn.setText(
                f"'{name}' isn't a cloze notetype — AI Create expects cloze cards. "
                "This is fine if you plan to use a custom notetype; just be aware "
                "the AI will generate cloze syntax."
            )

    def selected_deck(self) -> str:
        return self._deck.currentText().strip()

    def selected_notetype(self) -> str:
        return self._notetype.currentText().strip()

    def validate(self) -> bool:
        if not self.selected_deck():
            showWarning("Pick a deck for new cards.")
            return False
        if not self.selected_notetype():
            showWarning("Pick a notetype.")
            return False
        return True


class _PageTest(QWidget):
    """Page 4: test connection + generate one sample card."""

    def __init__(self, wizard: SetupWizard, parent=None):
        super().__init__(parent)
        self._wizard = wizard
        self._card_data: dict | None = None

        root = QVBoxLayout(self)
        root.setContentsMargins(24, 20, 24, 16)
        root.setSpacing(12)

        root.addWidget(_h2("Let's make sure it works"))
        root.addWidget(_muted(
            "Click <b>Test connection</b> below. If that passes, generate one "
            "sample card to confirm the full pipeline works before you start."
        ))
        root.addWidget(_hsep())

        # Connection test row
        test_row = QHBoxLayout()
        self._test_btn = QPushButton("Test connection")
        self._test_btn.setMinimumWidth(160)
        self._test_btn.clicked.connect(self._run_test)
        test_row.addWidget(self._test_btn)
        self._test_lbl = QLabel("")
        self._test_lbl.setStyleSheet("font-size: 12px;")
        test_row.addWidget(self._test_lbl, 1)
        root.addLayout(test_row)

        root.addWidget(_hsep())

        # Card generation
        gen_row = QHBoxLayout()
        self._gen_btn = QPushButton("Generate a test card")
        self._gen_btn.setMinimumWidth(180)
        self._gen_btn.setEnabled(False)
        self._gen_btn.clicked.connect(self._run_gen)
        gen_row.addWidget(self._gen_btn)
        self._gen_lbl = QLabel("")
        self._gen_lbl.setStyleSheet("color: gray; font-size: 12px;")
        gen_row.addWidget(self._gen_lbl, 1)
        root.addLayout(gen_row)

        # Card preview
        self._preview_box = QGroupBox("Generated card preview")
        self._preview_box.setVisible(False)
        pb_lay = QVBoxLayout(self._preview_box)
        pb_lay.setSpacing(6)
        self._front_view = QTextEdit()
        self._front_view.setReadOnly(True)
        self._front_view.setMaximumHeight(90)
        self._front_view.setPlaceholderText("Front field")
        self._extra_view = QTextEdit()
        self._extra_view.setReadOnly(True)
        self._extra_view.setMaximumHeight(60)
        self._extra_view.setPlaceholderText("Extra / back field")
        self._extra_view.setStyleSheet("color: gray;")
        pb_lay.addWidget(QLabel("<small><b>Front:</b></small>"))
        pb_lay.addWidget(self._front_view)
        pb_lay.addWidget(QLabel("<small><b>Extra:</b></small>"))
        pb_lay.addWidget(self._extra_view)

        add_row = QHBoxLayout()
        add_row.addStretch(1)
        self._add_btn = QPushButton("Add this card to Anki")
        self._add_btn.clicked.connect(self._add_card)
        self._add_btn.setEnabled(False)
        add_row.addWidget(self._add_btn)
        self._added_lbl = QLabel("")
        self._added_lbl.setStyleSheet("color: #1c8000; font-size: 11px;")
        add_row.addWidget(self._added_lbl)
        pb_lay.addLayout(add_row)
        root.addWidget(self._preview_box)

        root.addStretch(1)

        self._connection_ok = False

    def refresh(self) -> None:
        """Save current wizard choices to config before tests run."""
        self._wizard._apply_config()
        self._test_lbl.setText("")
        self._gen_lbl.setText("")
        self._test_btn.setEnabled(True)
        self._gen_btn.setEnabled(False)
        self._preview_box.setVisible(False)
        self._add_btn.setEnabled(False)
        self._added_lbl.setText("")
        self._connection_ok = False
        self._card_data = None

    def _run_test(self):
        from ..core import qt_utils
        reply = qt_utils.run_claude_text(
            self._test_btn,
            "Testing connection…",
            prompt="Reply with the word ok and nothing else.",
            system="You are a connection test. Reply with the single word: ok",
            max_tokens=16,
        )
        if reply and reply.strip().lower().startswith("ok"):
            self._test_lbl.setText("✓ Connection OK")
            self._test_lbl.setStyleSheet("color: #1c8000; font-size: 12px;")
            self._gen_btn.setEnabled(True)
            self._connection_ok = True
        elif reply:
            self._test_lbl.setText(f"Unexpected reply: {reply.strip()[:60]}")
            self._test_lbl.setStyleSheet("color: #b85c00; font-size: 12px;")
        else:
            self._test_lbl.setText("✗ No reply — check your settings")
            self._test_lbl.setStyleSheet("color: #c0392b; font-size: 12px;")

    def _run_gen(self):
        from ..core import qt_utils
        self._gen_lbl.setText("Generating…")
        self._preview_box.setVisible(False)
        self._add_btn.setEnabled(False)
        self._added_lbl.setText("")

        raw = qt_utils.run_claude_text(
            self._gen_btn,
            "Generating test card…",
            prompt=_TEST_CARD_PROMPT,
            system="You generate Anki cards. Follow the output format exactly.",
            max_tokens=400,
        )
        if not raw:
            self._gen_lbl.setText("Generation failed — check settings or try again.")
            self._gen_lbl.setStyleSheet("color: #c0392b;")
            return

        card = self._parse_card(raw)
        if not card:
            self._gen_lbl.setText(
                "Couldn't parse the response — model may have returned "
                "unexpected text. Try again."
            )
            self._gen_lbl.setStyleSheet("color: #b85c00;")
            return

        self._card_data = card
        self._front_view.setPlainText(card.get("front", ""))
        self._extra_view.setPlainText(card.get("extra", ""))
        self._preview_box.setVisible(True)
        self._add_btn.setEnabled(True)
        self._gen_lbl.setText("✓ Card generated")
        self._gen_lbl.setStyleSheet("color: #1c8000;")

    @staticmethod
    def _parse_card(raw: str) -> dict | None:
        """Extract the first {...} JSON object from the raw reply."""
        import re
        # Strip markdown fences if present.
        raw = re.sub(r"```[a-z]*\n?", "", raw).strip()
        # Try direct parse first.
        try:
            obj = json.loads(raw)
            if isinstance(obj, dict) and "front" in obj:
                return obj
        except json.JSONDecodeError:
            pass
        # Find the first JSON object by brace matching.
        start = raw.find("{")
        if start < 0:
            return None
        depth = 0
        for i, ch in enumerate(raw[start:], start):
            if ch == "{":
                depth += 1
            elif ch == "}":
                depth -= 1
                if depth == 0:
                    try:
                        obj = json.loads(raw[start : i + 1])
                        if isinstance(obj, dict) and "front" in obj:
                            return obj
                    except json.JSONDecodeError:
                        pass
                    break
        return None

    def _add_card(self):
        if not self._card_data:
            return
        deck_name = self._wizard.page_defaults.selected_deck()
        notetype_name = self._wizard.page_defaults.selected_notetype()
        if not deck_name or not notetype_name:
            tooltip("Set a deck and notetype on the previous page first.")
            return
        try:
            nt = mw.col.models.by_name(notetype_name)
            if nt is None:
                tooltip(f"Notetype '{notetype_name}' not found.")
                return
            note = mw.col.new_note(nt)
            fields = {f["name"]: i for i, f in enumerate(nt["flds"])}
            front_field = next(
                (n for n in ("Text", "Front") if n in fields),
                list(fields.keys())[0] if fields else None
            )
            extra_field = next(
                (n for n in ("Extra", "Back", "Notes") if n in fields),
                None
            )
            if front_field:
                note[front_field] = self._card_data.get("front", "")
            if extra_field:
                note[extra_field] = self._card_data.get("extra", "")
            deck_id = mw.col.decks.id(deck_name, create=True)
            mw.col.add_note(note, deck_id)
            mw.col.reset()
            self._add_btn.setEnabled(False)
            self._added_lbl.setText("✓ Added to collection")
        except Exception as e:
            log.error(f"wizard add card failed: {e}")
            tooltip(f"Failed to add card: {e}")

    def validate(self) -> bool:
        return True


# ── wizard ─────────────────────────────────────────────────────────────────────

class SetupWizard(QDialog):
    """Five-page setup wizard. Pages adapt to the chosen provider."""

    def __init__(self, parent=None):
        super().__init__(parent or mw)
        self.setWindowTitle("Ankisstant Setup")
        self.setMinimumWidth(720)
        self.setMinimumHeight(680)
        self.cfg = load_config()

        self._flow: list[int] = []
        self._flow_pos: int = 0

        self.current_provider: str = (self.cfg.get("provider") or "auto").lower()
        if self.current_provider == "auto":
            self.current_provider = "cli"

        self._build()

    # ── construction ──────────────────────────────────────────────────────────

    def _build(self):
        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)

        self._step_lbl = QLabel("")
        self._step_lbl.setAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
        self._step_lbl.setStyleSheet(
            "color: gray; font-size: 11px; padding: 4px 16px;"
        )
        root.addWidget(self._step_lbl)

        root.addWidget(_hsep())

        self._stack = QStackedWidget()
        self.page_intro    = _PageIntro(self)
        self.page_creds    = _PageCredentials(self)
        self.page_model    = _PageModel(self)
        self.page_defaults = _PageDefaults(self)
        self.page_test     = _PageTest(self)
        self._stack.addWidget(self.page_intro)    # _P_INTRO = 0
        self._stack.addWidget(self.page_creds)    # _P_CREDS = 1
        self._stack.addWidget(self.page_model)    # _P_MODEL = 2
        self._stack.addWidget(self.page_defaults) # _P_DEFAULTS = 3
        self._stack.addWidget(self.page_test)     # _P_TEST = 4
        root.addWidget(self._stack, 1)

        root.addWidget(_hsep())

        nav = QHBoxLayout()
        nav.setContentsMargins(16, 10, 16, 14)
        nav.setSpacing(8)

        self._skip_btn = QPushButton("Skip setup")
        self._skip_btn.setFlat(True)
        self._skip_btn.setStyleSheet("color: gray;")
        self._skip_btn.clicked.connect(self._on_skip)
        nav.addWidget(self._skip_btn)

        nav.addStretch(1)

        self._back_btn = QPushButton("← Back")
        self._back_btn.setMinimumWidth(90)
        self._back_btn.clicked.connect(self._go_back)
        nav.addWidget(self._back_btn)

        self._next_btn = QPushButton("Next →")
        self._next_btn.setMinimumWidth(110)
        self._next_btn.setDefault(True)
        self._next_btn.clicked.connect(self._go_next)
        nav.addWidget(self._next_btn)

        root.addLayout(nav)

        self._enter_page(_P_INTRO)

    # ── navigation ────────────────────────────────────────────────────────────

    def _page_flow_for(self, provider: str) -> list[int]:
        if provider == "manual":
            return [_P_INTRO, _P_CREDS, _P_DEFAULTS, _P_TEST]
        return [_P_INTRO, _P_CREDS, _P_MODEL, _P_DEFAULTS, _P_TEST]

    def _enter_page(self, page_idx: int):
        self._stack.setCurrentIndex(page_idx)
        if page_idx == _P_CREDS:
            self.page_creds.refresh()
        elif page_idx == _P_MODEL:
            self.page_model.refresh()
        elif page_idx == _P_TEST:
            self.page_test.refresh()

        pos = self._flow_pos + 1 if self._flow else 1
        total = len(self._flow) if self._flow else 5
        self._step_lbl.setText(f"Step {pos} of {total}")

        self._back_btn.setVisible(page_idx != _P_INTRO)

        if page_idx == _P_TEST:
            self._next_btn.setText("Finish")
        elif page_idx == _P_DEFAULTS and _P_TEST not in self._flow:
            self._next_btn.setText("Finish")
        else:
            self._next_btn.setText("Next →")

        self._skip_btn.setVisible(page_idx == _P_INTRO)

    def _go_next(self):
        current_page = self._stack.currentIndex()
        current_widget = self._stack.currentWidget()

        if not current_widget.validate():
            return

        if current_page == _P_INTRO:
            self.current_provider = self.page_intro.selected_provider()
            self._flow = self._page_flow_for(self.current_provider)
            self._flow_pos = 0

        if self._flow_pos < len(self._flow) - 1:
            self._flow_pos += 1
            next_page = self._flow[self._flow_pos]
            self._enter_page(next_page)
        else:
            self._finish()

    def _go_back(self):
        if self._flow_pos > 0:
            self._flow_pos -= 1
            self._enter_page(self._flow[self._flow_pos])

    # ── config application ────────────────────────────────────────────────────

    def _apply_config(self):
        """Write wizard choices into self.cfg and persist to disk."""
        provider = self.current_provider
        self.cfg["provider"] = provider

        if provider == "cli":
            path = self.page_creds.get_cli_path()
            if path:
                self.cfg["claude_cli_path"] = path
        elif provider == "anthropic":
            key = self.page_creds.get_api_key()
            if key:
                self.cfg["anthropic_api_key"] = key
        elif provider == "gemini":
            key = self.page_creds.get_api_key()
            if key:
                self.cfg["gemini_api_key"] = key
        elif provider == "openai":
            key = self.page_creds.get_api_key()
            if key:
                self.cfg["openai_api_key"] = key
        elif provider == "ollama":
            url = self.page_creds.get_ollama_url()
            if url:
                self.cfg["ollama_base_url"] = url

        if provider != "manual" and _P_MODEL in self._flow:
            model = self.page_model.selected_model()
            if model:
                fam = family_for(provider)
                self.cfg.setdefault("model_defaults", {})[fam] = model

        deck     = self.page_defaults.selected_deck()
        notetype = self.page_defaults.selected_notetype()

        tools = self.cfg.setdefault("tools", {})
        cc = tools.setdefault("card_creator", {})
        if deck:
            cc["default_deck"] = deck
        if notetype:
            cc["default_notetype"] = notetype

        qb = tools.setdefault("qbank", {})
        if deck and not qb.get("card_deck"):
            qb["card_deck"] = deck
        if notetype and not qb.get("card_notetype"):
            qb["card_notetype"] = notetype

        save_config(self.cfg)

    def _finish(self):
        self._apply_config()
        self.cfg["first_run_seen"] = True
        save_config(self.cfg)
        tooltip("Setup saved — opening Ankisstant…")
        self.accept()
        try:
            from .main_window import open_main_window
            open_main_window()
        except Exception as e:
            log.warn(f"wizard: failed to open main window: {e}")

    def _on_skip(self):
        self.cfg["first_run_seen"] = True
        save_config(self.cfg)
        self.reject()


# Keep old name as alias so Settings → About still works.
WelcomeDialog = SetupWizard


def open_welcome() -> None:
    """Public entry — called by __init__.py on first run and by Settings → About."""
    dlg = SetupWizard()
    dlg.exec()
