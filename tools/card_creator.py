# Create Cards with Claude — propose cloze cards from source or topic, review,
# then create or open in Add Screen.
#
# Exposes init() and get_panel() per the Ankisstant tool contract.

from __future__ import annotations

import html
import json
import os
import re
import urllib.request
import urllib.error
import xml.etree.ElementTree as ET
import zipfile
from datetime import datetime as _dt

from aqt import mw, gui_hooks
from aqt.qt import (
    QApplication, QButtonGroup, QComboBox, QDialog, QDialogButtonBox, QFileDialog,
    QFrame, QFormLayout, QGroupBox, QHBoxLayout, QLabel, QLineEdit, QListWidget,
    QListWidgetItem, QPlainTextEdit, QPushButton, QRadioButton, QScrollArea,
    QSpinBox, Qt, QTimer, QVBoxLayout, QWidget,
)
from aqt.utils import askUser, showWarning, tooltip

from ..core import anki_utils, api as core_api, log
from ..core.config import load_config, tool_config, save_tool_config
from ..core.qt_utils import (
    attach_tag_completer, loading, make_help_button, make_setup_banner,
    provider_configured,
)


NAME = "Create with Claude"


# ── prompts ───────────────────────────────────────────────────────────────────

CARD_GEN_SYSTEM = """You generate high-yield Anki cloze cards for a Year 3 Australian medical student.

OUTPUT FORMAT
Return ONLY a JSON array. Each element:
  {"front": "<cloze text with {{c1::...}} / {{c2::...}} etc>",
   "extra": "<supporting clinical context, mnemonics, why-it-matters>"}

CARD RULES (derived from Wozniak's 20 Rules / Med School Insiders best practices)
- MINIMUM INFORMATION PRINCIPLE: one atomic fact per card. If a card tests more than one thing, split it. Simple cards are easier to review, easier to schedule, and don't fail-cascade.
- CONCISION: ruthless. Cut every word that doesn't change the meaning. No filler ("important to know that…", "remember that…"). At hundreds of reviews/day, every extra word costs.
- Use standard Anki cloze syntax: {{c1::answer}}, {{c2::answer}}.
  - Prefer SHORT cloze answers (1–4 words). Long clozed phrases are hard to grade honestly.
  - Multiple clozes per card are fine ONLY if they test tightly-coupled facts (e.g. drug + class + mechanism). Otherwise split into separate cards.
  - Use {{c1::}}, {{c2::}}, … to make sibling cards; use the SAME number (e.g. {{c1::A}} … {{c1::B}}) only when you want them revealed together as one card.
- AVOID ENUMERATIONS / SETS: never make a single card with "list the 5 causes of X". Either pick the 1–2 highest-yield items per card, or use overlapping/sequential clozes.
- LEVEL: Year 3 Australian medical student. Australian drug names and guidelines (e.g. eTG, RACGP) preferred when relevant.
- FORMATTING: <b> for high-yield terms, <u> for underline. No other HTML.
- EXTRA: clinical context, mnemonic, or one-line "why it matters". Do not repeat the front. Skip if you have nothing meaningful to add.
- UNDERSTAND BEFORE MEMORIZE: don't cloze a number / classification the student can't make sense of. If a fact only makes sense with framing, put the framing in the front (unclozed) and cloze the testable part.

DO NOT include any prose outside the JSON array. No markdown fences. Just the JSON array."""

SINGLE_REGEN_SYSTEM = (
    "Regenerate ONE Anki cloze card on the same subject as the card supplied. "
    "Return a JSON object with keys 'front' and 'extra' only. Same rules as before: "
    "standard {{c1::...}} cloze syntax, <b>/<u> only, no markdown fences, no prose. "
    "Honour the minimum information principle and keep cloze answers SHORT (1–4 words)."
)

SPLIT_SYSTEM = (
    "Split the supplied Anki card into multiple ATOMIC single-cloze cards. Each output card "
    "must test ONE fact, contain exactly ONE cloze deletion ({{c1::...}}), and stand on its own. "
    "Keep cloze answers short (1–4 words). Preserve the clinical meaning of the original. "
    "Return ONLY a JSON array of {front, extra} objects — no prose, no markdown fences."
)

def _augment_system(base: str, profile: dict | None) -> str:
    """Append a NOTETYPE block to a system prompt so Claude knows which
    notetype the output will populate, which fields are available, and
    any user-supplied per-notetype style guidance (e.g. Malleus style).

    The skill file itself is NOT inlined here — instead, the CLI is told
    to Read it on demand (see _resolve_skill), keeping the system prompt
    small. API mode uses the server-side skills beta via `skill_id`."""
    if not profile:
        return base
    extra: list[str] = ["", "NOTETYPE", f"You are writing cards for the '{profile.get('name', '?')}' notetype."]
    extra.append(
        f"The student will store the card front in <{profile.get('front_field', 'Text')}> "
        f"and supporting text in <{profile.get('extra_field', 'Extra')}>."
    )
    nt = None
    try:
        nt = mw.col.models.by_name(profile.get("name", ""))
    except Exception:
        nt = None
    if nt is not None:
        fields = [f["name"] for f in nt.get("flds", [])]
        if fields:
            extra.append("All available fields on this notetype: " + ", ".join(fields) + ".")
    sources_field = (profile.get("sources_field") or "").strip()
    if sources_field:
        extra.append(
            f"This notetype has a dedicated source/citation field <{sources_field}>. "
            "Include a 'source' key in each JSON object alongside 'front' and 'extra' — "
            "its value will be written verbatim to that field. Use HTML "
            "(e.g. <a href='…'>title</a>, <br> between citations) when appropriate."
        )
    instructions = (profile.get("extra_instructions") or "").strip()
    if instructions:
        extra.append("Additional style instructions for this notetype:")
        extra.append(instructions)
    extra.append(
        "Do NOT change the JSON shape — still return {\"front\": ..., \"extra\": ...} "
        "(or an array of them, depending on the original instruction). The 'front' key "
        "always maps to the front field, 'extra' to the extra field."
    )
    return base + "\n" + "\n".join(extra)


ONE_BY_ONE_SYSTEM = (
    "Rewrite the supplied Anki card as a SINGLE card whose cloze deletions are revealed one "
    "at a time during review. Every cloze MUST use the same number — {{c1::...}} — so they "
    "act as siblings. The card will be paired with the AnKing 'One by one' field set to 'y', "
    "which causes the AnKing reviewer JS to reveal each c1 cloze in sequence. "
    "Rules:\n"
    "- Output ONE JSON object: {\"front\": ..., \"extra\": ...}.\n"
    "- ALL cloze deletions in front must be {{c1::...}} (no c2, c3, …).\n"
    "- Order the clozes in the sequence the student should reveal them.\n"
    "- Keep each cloze answer short (1–4 words).\n"
    "- Preserve the original clinical meaning; don't add new facts.\n"
    "- No prose outside the JSON object. No markdown fences."
)


# ── small helpers ────────────────────────────────────────────────────────────

def _profile_for(cfg: dict, name: str) -> dict | None:
    """Look up a notetype profile by name from the config. Returns None if no
    profile exists; callers should fall back to the legacy top-level fields."""
    for p in cfg.get("notetypes", []) or []:
        if p.get("name") == name:
            return p
    return None


def _resolved_profile(cfg: dict, name: str) -> dict:
    """Return a profile dict for `name`, synthesising one from the legacy
    top-level field names if no curated profile exists. The synthesised
    profile has no extra_instructions."""
    p = _profile_for(cfg, name)
    if p:
        return p
    return {
        "name":               name,
        "front_field":        cfg.get("front_field", "Text"),
        "extra_field":        cfg.get("extra_field", "Extra"),
        "image_field":        cfg.get("extra_field", "Extra"),
        "one_by_one_field":   cfg.get("one_by_one_field", "One by one"),
        "extra_instructions": "",
    }


def _resolve_skill(profile: dict | None) -> tuple[str, str]:
    """Decide which skill to apply for a card-creation request.

    Returns (skill_invocation, api_skill_id):
      - skill_invocation → free-text prefix prepended to the CLI prompt,
                           e.g. '/malleus-anki' or 'Use the malleus-anki
                           skill'. The SKILL.md body itself lives in
                           ~/.claude/skills/<name>/ and is auto-loaded by
                           Claude Code on demand.
      - api_skill_id     → Anthropic custom skill ID passed to the skills
                           beta (forces the API path in api.py).

    Mode rules:
      - provider_mode=cli  → invocation only (ignore API ID).
      - provider_mode=api  → API skill ID only (ignore invocation).
      - provider_mode=auto → if API ID is set, use it (forces API path);
                            otherwise fall back to the CLI invocation.
    """
    if not profile:
        return ("", "")
    cfg = load_config()
    mode = (cfg.get("provider_mode") or "auto").lower()
    skill_id = (profile.get("card_creation_skill_id") or "").strip()
    invocation = (profile.get("card_creation_skill_invocation") or "").strip()
    if mode == "cli":
        return (invocation, "")
    if mode == "api":
        if invocation and not skill_id:
            log.warning(
                f"card_creator: profile '{profile.get('name', '?')}' has a CLI "
                "skill invocation but provider_mode=api; upload the skill to "
                "Anthropic and paste its skill_… ID to activate it."
            )
        return ("", skill_id)
    # auto
    if skill_id:
        return ("", skill_id)
    return (invocation, "")


def _import_image_to_media(path: str) -> str | None:
    """Copy an image into Anki's media folder; return the stored filename
    (which may differ from the original basename if there's a collision)."""
    try:
        return mw.col.media.add_file(path)
    except Exception as e:
        print(f"[ankisstant] media.add_file failed for {path}: {e}")
        return None


def _img_tags_for(paths: list[str]) -> str:
    """Import a list of image paths into the media folder and return an
    HTML fragment of <img> tags (one per line) to append to a field."""
    bits: list[str] = []
    for p in paths or []:
        fname = _import_image_to_media(p)
        if not fname:
            continue
        bits.append(f'<img src="{html.escape(fname, quote=True)}">')
    return "<br>".join(bits)


def _fetch_url(url: str) -> str:
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0 Ankisstant"})
    with urllib.request.urlopen(req, timeout=30) as resp:
        raw = resp.read().decode("utf-8", errors="replace")
    text = re.sub(r"<script[\s\S]*?</script>", " ", raw, flags=re.I)
    text = re.sub(r"<style[\s\S]*?</style>", " ", text, flags=re.I)
    text = anki_utils.strip_html(text)
    return text[:30000]  # Claude does fine with ~30k chars


# OOXML drawing-text namespace, used by both PPTX slides and notes.
_PPTX_A_NS = "{http://schemas.openxmlformats.org/drawingml/2006/main}"


def _natural_key(name: str):
    """Sort 'slide2.xml' before 'slide10.xml'."""
    return [int(c) if c.isdigit() else c for c in re.split(r"(\d+)", name)]


def _extract_pptx_text(path: str) -> str:
    """Pull text out of a .pptx without external deps: it's a zip of XML.
    Walks each slide in order, then notes."""
    out: list[str] = []
    with zipfile.ZipFile(path) as zf:
        slide_names = sorted(
            (n for n in zf.namelist()
             if n.startswith("ppt/slides/slide") and n.endswith(".xml")),
            key=_natural_key,
        )
        note_names = sorted(
            (n for n in zf.namelist()
             if n.startswith("ppt/notesSlides/notesSlide") and n.endswith(".xml")),
            key=_natural_key,
        )
        for i, name in enumerate(slide_names, 1):
            with zf.open(name) as fh:
                try:
                    root = ET.parse(fh).getroot()
                except ET.ParseError:
                    continue
            paragraphs: list[str] = []
            for para in root.iter(_PPTX_A_NS + "p"):
                runs = [t.text or "" for t in para.iter(_PPTX_A_NS + "t")]
                line = "".join(runs).strip()
                if line:
                    paragraphs.append(line)
            if paragraphs:
                out.append(f"\n## Slide {i}\n" + "\n".join(paragraphs))
        for name in note_names:
            with zf.open(name) as fh:
                try:
                    root = ET.parse(fh).getroot()
                except ET.ParseError:
                    continue
            texts = [(t.text or "").strip() for t in root.iter(_PPTX_A_NS + "t")]
            joined = "\n".join(t for t in texts if t)
            if joined.strip():
                out.append(f"\n## Notes ({os.path.basename(name)})\n{joined}")
    return "\n".join(out).strip()


def _log_path() -> str:
    here = os.path.dirname(os.path.abspath(__file__))
    root = os.path.dirname(here)
    uf = os.path.join(root, "user_files")
    os.makedirs(uf, exist_ok=True)
    return os.path.join(uf, "card_creation_log.json")


def _append_session(mode, source, topic, cards_proposed, cards_created, card_ids):
    entry = {
        "timestamp": _dt.now().isoformat(timespec="seconds"),
        "mode": mode,
        "source": source,
        "topic": topic,
        "cards_proposed": cards_proposed,
        "cards_created": cards_created,
        "card_ids": card_ids,
    }
    path = _log_path()
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        if not isinstance(data, list):
            data = []
    except (FileNotFoundError, json.JSONDecodeError):
        data = []
    data.append(entry)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)


# ── input panel ──────────────────────────────────────────────────────────────

class CreatorPanel(QWidget):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.cfg = tool_config("card_creator")
        # Set by refresh_queue_state when the panel is driven by Browse's
        # LO-gap queue. self._current_gap holds the gap text we pre-filled
        # the form with; we pop it from the queue only on a successful
        # ReviewDialog acceptance.
        self._main_window = None
        # _current_gap is the dict at queue[0] we pre-filled the form with —
        # popped only on a successful ReviewDialog acceptance.
        self._current_gap: dict | None = None
        self._build()
        self._rebuild_queue_view()

    @staticmethod
    def _gap_title(gap) -> str:
        if isinstance(gap, dict):
            return gap.get("title", "") or ""
        return str(gap or "")

    def _build(self):
        root = QVBoxLayout(self)
        root.setContentsMargins(16, 16, 16, 16)
        root.setSpacing(8)

        self._setup_banner = make_setup_banner(self)
        root.addWidget(self._setup_banner)
        self.refresh_setup_banner()

        title_row = QHBoxLayout()
        title = QLabel("<h2 style='margin:0'>Create with Claude</h2>")
        title.setTextFormat(Qt.TextFormat.RichText)
        title_row.addWidget(title)
        title_row.addStretch(1)
        title_row.addWidget(make_help_button(
            "Create with Claude — help",
            "<h3>What it does</h3>"
            "<p>Draft Anki cloze cards from a topic, pasted text, a URL, "
            "or attached PDFs / PowerPoints. Review each card before adding.</p>"
            "<h3>Input modes</h3>"
            "<ul>"
            "<li><b>From topic</b> — Claude writes cards from scratch on a topic.</li>"
            "<li><b>From source</b> — paste text, fetch a URL, or attach a PDF / PPTX. "
            "PDFs are passed to Claude directly. PPTX files have their text "
            "extracted locally (image-only slides won't work).</li>"
            "</ul>"
            "<h3>Workflow</h3>"
            "<ol>"
            "<li>Pick mode, set focus / tags, choose deck.</li>"
            "<li>Click <b>Generate</b>. Review each draft in the dialog.</li>"
            "<li>Approve / split / regenerate cards. Then either:</li>"
            "<ul>"
            "<li><b>Create all</b> — adds directly to the deck.</li>"
            "<li><b>Open Add screen</b> — feeds them one by one into Anki's "
            "Add Cards window so you can tweak before adding. Closing the Add "
            "window stops the queue.</li>"
            "</ul></ol>"
            "<h3>Settings</h3>"
            "<p>Default deck, notetype, audit tag, and field names are in "
            "<b>Ankisstant Settings → Create with Claude</b>.</p>",
            self,
        ))
        root.addLayout(title_row)

        # ── Queue panel — shows ALL queued LO gaps, lets the user add / remove
        # / reorder, and pre-fills the form below from the top item. ──────────
        self.queue_box = QFrame()
        self.queue_box.setObjectName("queueBox")
        self.queue_box.setFrameShape(QFrame.Shape.StyledPanel)
        self.queue_box.setStyleSheet(
            "QFrame#queueBox { background: rgba(80,160,255,0.16); "
            "border: 1px solid rgba(80,160,255,0.55); border-radius: 6px; }"
        )
        qbl = QVBoxLayout(self.queue_box)
        qbl.setContentsMargins(10, 8, 10, 8)
        qbl.setSpacing(5)

        self.queue_header = QLabel()
        self.queue_header.setStyleSheet("font-weight: 600; color: palette(text);")
        self.queue_header.setTextFormat(Qt.TextFormat.RichText)
        self.queue_header.setWordWrap(True)
        qbl.addWidget(self.queue_header)

        self.queue_list = QListWidget()
        self.queue_list.setMaximumHeight(140)
        self.queue_list.setStyleSheet(
            "QListWidget { background: transparent; border: none; color: palette(text); }"
            "QListWidget::item:selected { background: rgba(80,160,255,0.30); color: palette(text); }"
        )
        qbl.addWidget(self.queue_list)

        # Manual-add row.
        add_row = QHBoxLayout()
        add_row.addWidget(QLabel("Add gap:"))
        self.queue_add_input = QLineEdit()
        self.queue_add_input.setMinimumWidth(420)
        self.queue_add_input.setPlaceholderText("Type a gap statement and press Enter to queue it.")
        self.queue_add_input.returnPressed.connect(self._on_add_gap_manually)
        add_row.addWidget(self.queue_add_input, 1)
        self.queue_add_btn = QPushButton("+ Add")
        self.queue_add_btn.setAutoDefault(False)
        self.queue_add_btn.clicked.connect(self._on_add_gap_manually)
        add_row.addWidget(self.queue_add_btn)
        qbl.addLayout(add_row)

        # Action buttons.
        queue_btn_row = QHBoxLayout()
        self.queue_remove_btn = QPushButton("Remove selected")
        self.queue_remove_btn.setAutoDefault(False)
        self.queue_remove_btn.clicked.connect(self._on_remove_selected_gap)
        queue_btn_row.addWidget(self.queue_remove_btn)
        queue_btn_row.addStretch(1)
        self.queue_skip_btn = QPushButton("Skip current")
        self.queue_skip_btn.setAutoDefault(False)
        self.queue_skip_btn.clicked.connect(self._on_skip_gap)
        self.queue_clear_btn = QPushButton("Clear queue")
        self.queue_clear_btn.setAutoDefault(False)
        self.queue_clear_btn.clicked.connect(self._on_clear_queue)
        queue_btn_row.addWidget(self.queue_skip_btn)
        queue_btn_row.addWidget(self.queue_clear_btn)
        qbl.addLayout(queue_btn_row)

        root.addWidget(self.queue_box)

        # Mode toggle
        mode_box = QGroupBox("Mode")
        ml = QHBoxLayout(mode_box)
        self.rb_source = QRadioButton("Source-based (more reliable)")
        self.rb_topic = QRadioButton("Topic-based (verify carefully)")
        self.rb_source.setChecked(True)
        grp = QButtonGroup(self)
        grp.addButton(self.rb_source)
        grp.addButton(self.rb_topic)
        ml.addWidget(self.rb_source)
        ml.addWidget(self.rb_topic)
        ml.addStretch(1)
        root.addWidget(mode_box)

        # Source area
        self.source_box = QGroupBox("Source")
        sl = QVBoxLayout(self.source_box)
        sl.addWidget(QLabel("Paste text below, enter a URL, and/or attach PDFs / PowerPoints."))
        self.url = QLineEdit()
        self.url.setMinimumWidth(500)
        self.url.setPlaceholderText("https://… (optional)")
        sl.addWidget(self.url)
        self.text = QPlainTextEdit()
        self.text.setMinimumHeight(140)
        self.text.setPlaceholderText("Paste lecture notes / textbook excerpt / etc.")
        sl.addWidget(self.text, 1)

        # Attachments row
        self._attached_paths: list[str] = []
        attach_row = QHBoxLayout()
        self.attach_btn = QPushButton("📎 Attach PDF / PPTX…")
        self.attach_btn.setAutoDefault(False)
        self.attach_btn.clicked.connect(self._on_attach_files)
        self.attach_clear_btn = QPushButton("Clear")
        self.attach_clear_btn.setAutoDefault(False)
        self.attach_clear_btn.clicked.connect(self._on_attach_clear)
        self.attach_clear_btn.setVisible(False)
        attach_row.addWidget(self.attach_btn)
        attach_row.addWidget(self.attach_clear_btn)
        attach_row.addStretch(1)
        sl.addLayout(attach_row)

        self.attach_list = QListWidget()
        self.attach_list.setMaximumHeight(80)
        self.attach_list.setStyleSheet(
            "QListWidget { background: transparent; color: palette(text); "
            "border: 1px dashed palette(mid); border-radius: 4px; }"
        )
        self.attach_list.setVisible(False)
        sl.addWidget(self.attach_list)
        root.addWidget(self.source_box, 1)

        # Topic field
        self.topic_box = QGroupBox("Topic")
        tl = QVBoxLayout(self.topic_box)
        tl.addWidget(QLabel("Topic (Claude generates from its own knowledge — verify before exam):"))
        self.topic = QLineEdit()
        self.topic.setMinimumWidth(500)
        tl.addWidget(self.topic)
        warn = QLabel("⚠ Topic-based cards carry more hallucination risk. Always verify.")
        warn.setStyleSheet("color: #b85c00;")
        tl.addWidget(warn)
        root.addWidget(self.topic_box)
        self.topic_box.setVisible(False)

        self.rb_source.toggled.connect(self._sync_mode)
        self.rb_topic.toggled.connect(self._sync_mode)

        # Spec
        spec_box = QGroupBox("Card spec")
        f = QFormLayout(spec_box)
        f.setFieldGrowthPolicy(QFormLayout.FieldGrowthPolicy.ExpandingFieldsGrow)
        self.n_cards = QSpinBox()
        self.n_cards.setRange(1, 40)
        self.n_cards.setValue(int(self.cfg.get("default_n_cards", 10)))
        f.addRow("Number of cards:", self.n_cards)

        # Notetype dropdown — populated from the curated profile list in
        # settings. We persist the last choice into `selected_notetype` so
        # it survives a panel rebuild. The legacy `default_notetype` is
        # honoured as a seed when nothing else is set.
        self.notetype = QComboBox()
        self._rebuild_notetype_combo()
        # Settings link so users can add profiles without leaving the panel.
        nt_row = QHBoxLayout()
        nt_row.addWidget(self.notetype, 1)
        self.notetype_settings_btn = QPushButton("Manage…")
        self.notetype_settings_btn.setAutoDefault(False)
        self.notetype_settings_btn.setToolTip(
            "Open Ankisstant Settings → Create with Claude to add or edit notetype profiles."
        )
        self.notetype_settings_btn.clicked.connect(self._on_open_notetype_settings)
        nt_row.addWidget(self.notetype_settings_btn)
        f.addRow("Notetype:", nt_row)
        self.notetype.currentIndexChanged.connect(self._on_notetype_changed)

        self.deck = QComboBox()
        self.deck.setEditable(True)
        if mw.col is not None:
            deck_names = sorted(d.name for d in mw.col.decks.all_names_and_ids())
            for n in deck_names:
                self.deck.addItem(n)
        default_deck = self.cfg.get("default_deck", "")
        if default_deck:
            idx = self.deck.findText(default_deck)
            if idx >= 0:
                self.deck.setCurrentIndex(idx)
            else:
                self.deck.setEditText(default_deck)
        f.addRow("Deck:", self.deck)

        self.tags = QLineEdit(", ".join(self.cfg.get("default_tags", [])))
        self.tags.setMinimumWidth(500)
        attach_tag_completer(self.tags, multi=True)
        f.addRow("Tags (comma-sep):", self.tags)
        audit_tag = self.cfg.get("audit_tag", "")
        if audit_tag:
            audit_hint = QLabel(
                f"<small>Audit tag <code>{audit_tag}</code> is applied automatically "
                "to every card created.</small>"
            )
            audit_hint.setTextFormat(Qt.TextFormat.RichText)
            audit_hint.setStyleSheet("color: gray;")
            f.addRow("", audit_hint)

        self.focus = QLineEdit()
        self.focus.setMinimumWidth(500)
        self.focus.setPlaceholderText("e.g. focus on clinical presentation not mechanism")
        f.addRow("Focus (optional):", self.focus)

        # Panel-level images — copied into Anki media and appended to every
        # approved card's image field at create time. Per-card overrides are
        # added in the review dialog (see _CardRow).
        self._extra_image_paths: list[str] = []
        img_row = QHBoxLayout()
        self.extra_img_btn = QPushButton("📷 Attach images for Extra…")
        self.extra_img_btn.setAutoDefault(False)
        self.extra_img_btn.setToolTip(
            "Pick image files (PNG/JPG/etc). They'll be added to the Extra field "
            "of every card created in this batch. Useful for flowcharts / guideline "
            "excerpts that apply to all generated cards."
        )
        self.extra_img_btn.clicked.connect(self._on_attach_extra_images)
        self.extra_img_clear_btn = QPushButton("Clear")
        self.extra_img_clear_btn.setAutoDefault(False)
        self.extra_img_clear_btn.clicked.connect(self._on_clear_extra_images)
        self.extra_img_clear_btn.setVisible(False)
        img_row.addWidget(self.extra_img_btn)
        img_row.addWidget(self.extra_img_clear_btn)
        img_row.addStretch(1)
        f.addRow("Extra images:", img_row)

        self.extra_img_list = QListWidget()
        self.extra_img_list.setMaximumHeight(70)
        self.extra_img_list.setStyleSheet(
            "QListWidget { background: transparent; color: palette(text); "
            "border: 1px dashed palette(mid); border-radius: 4px; }"
        )
        self.extra_img_list.setVisible(False)
        f.addRow("", self.extra_img_list)

        root.addWidget(spec_box)

        # Buttons
        btn_row = QHBoxLayout()
        btn_row.addStretch(1)
        self.go_btn = QPushButton("Generate")
        self.go_btn.setDefault(True)
        self.go_btn.clicked.connect(self._on_generate)
        btn_row.addWidget(self.go_btn)
        root.addLayout(btn_row)

    def _sync_mode(self):
        self.source_box.setVisible(self.rb_source.isChecked())
        self.topic_box.setVisible(self.rb_topic.isChecked())

    # ── notetype dropdown ────────────────────────────────────────────────────

    def _rebuild_notetype_combo(self) -> None:
        """Reload the notetype dropdown from the configured profile list.
        Falls back to a single legacy entry if no profiles are configured.
        Restores the previously-selected notetype where possible."""
        # Block signals so the rebuild itself doesn't trigger save side-effects.
        self.notetype.blockSignals(True)
        self.notetype.clear()
        profiles = self.cfg.get("notetypes") or []
        if not profiles:
            legacy = (self.cfg.get("default_notetype") or "").strip()
            if legacy:
                self.notetype.addItem(legacy)
            else:
                self.notetype.addItem("(no notetype configured)")
                self.notetype.setEnabled(False)
        else:
            self.notetype.setEnabled(True)
            for p in profiles:
                name = p.get("name", "")
                label = name
                if p.get("extra_instructions"):
                    label = f"{name}  ✦"  # marker for custom prompt
                self.notetype.addItem(label, name)
        # Restore selection.
        selected = (self.cfg.get("selected_notetype")
                    or self.cfg.get("default_notetype") or "").strip()
        if selected:
            for i in range(self.notetype.count()):
                data = self.notetype.itemData(i) or self.notetype.itemText(i)
                if data == selected:
                    self.notetype.setCurrentIndex(i)
                    break
        self.notetype.blockSignals(False)

    def _current_notetype_name(self) -> str:
        data = self.notetype.itemData(self.notetype.currentIndex())
        if data:
            return data
        return self.notetype.currentText().strip()

    def _on_notetype_changed(self, _idx: int) -> None:
        name = self._current_notetype_name()
        if not name or name.startswith("("):
            return
        self.cfg["selected_notetype"] = name
        save_tool_config("card_creator", self.cfg)

    def _on_open_notetype_settings(self) -> None:
        try:
            from ..ui.settings import open_settings
            open_settings()
        except Exception as e:
            showWarning(f"Couldn't open settings: {e}")
            return
        # Reload config + rebuild combo so newly-added profiles appear.
        self.cfg = tool_config("card_creator")
        self._rebuild_notetype_combo()

    # ── panel-level image attachment for Extra ───────────────────────────────

    def _on_attach_extra_images(self):
        paths, _ = QFileDialog.getOpenFileNames(
            self, "Attach images for Extra",
            "", "Images (*.png *.jpg *.jpeg *.gif *.webp *.bmp *.svg);;All files (*)",
        )
        if not paths:
            return
        added = 0
        for p in paths:
            if p in self._extra_image_paths:
                continue
            ext = os.path.splitext(p)[1].lower()
            if ext not in (".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp", ".svg"):
                tooltip(f"Skipped {os.path.basename(p)} — not an image type.")
                continue
            self._extra_image_paths.append(p)
            added += 1
        if added:
            self._refresh_extra_image_list()

    def _on_clear_extra_images(self):
        self._extra_image_paths.clear()
        self._refresh_extra_image_list()

    def _refresh_extra_image_list(self):
        self.extra_img_list.clear()
        for p in self._extra_image_paths:
            self.extra_img_list.addItem(QListWidgetItem("🖼 " + os.path.basename(p)))
        has = bool(self._extra_image_paths)
        self.extra_img_list.setVisible(has)
        self.extra_img_clear_btn.setVisible(has)

    # ── LO-gap queue integration ─────────────────────────────────────────────
    #
    # Called by MainWindow whenever this panel is shown. Pre-fills the form
    # with the next queued gap; leaves the form alone if the queue is empty.

    def refresh_setup_banner(self) -> None:
        try:
            self._setup_banner.setVisible(not provider_configured())
        except Exception:
            pass

    def refresh_queue_state(self, main_window) -> None:
        self._main_window = main_window
        self._rebuild_queue_view()
        queue = getattr(main_window, "gap_queue", []) or []
        if not queue:
            self._current_gap = None
            return

        gap = queue[0]
        if gap == self._current_gap:
            return  # already pre-filled with this gap

        gap_text = self._gap_title(gap)
        # Only pre-fill if the user hasn't typed their own content in the
        # topic field. Tracking `_current_gap` lets the pop-on-success check
        # know whether this generation came from the queue.
        current_topic = self.topic.text().strip()
        prev_title = self._gap_title(self._current_gap)
        can_overwrite = (not current_topic) or (current_topic == prev_title)
        if not can_overwrite:
            self._current_gap = None
            return
        self._current_gap = gap
        try:
            self.rb_topic.setChecked(True)
            self._sync_mode()
            self.topic.setText(gap_text)
            focus_bits = [gap_text]
            if isinstance(gap, dict):
                # Strip HTML from any captured stem so we can surface the
                # underlying question text as a focus hint. (Screenshots
                # can't be carried through this path; the qbank-source
                # "Create from KG" flow bypasses gap_queue and uses
                # open_add_card_prefilled with the stem html intact.)
                stem = gap.get("stem_html") or ""
                notes = gap.get("notes") or ""
                if stem:
                    import re as _re
                    plain = _re.sub(r"<[^>]+>", " ", stem)
                    plain = _re.sub(r"\s+", " ", plain).strip()
                    if plain:
                        focus_bits.append("Context from source question: " + plain[:400])
                if notes:
                    focus_bits.append("Notes: " + notes[:300])
            self.focus.setText(" — ".join(focus_bits))
            self.n_cards.setValue(int(self.cfg.get("gap_n_cards", 3)))
        except Exception as e:
            print(f"[ankisstant] refresh_queue_state pre-fill failed: {e}")

    def _rebuild_queue_view(self) -> None:
        """Rebuild the queue list widget from the main-window queue. Does NOT
        touch the form fields. Queue panel stays visible even when empty so
        the user can manually add gaps."""
        queue = (
            getattr(self._main_window, "gap_queue", []) if self._main_window else []
        ) or []
        self.queue_list.clear()
        self.queue_box.setVisible(True)
        if not queue:
            self.queue_header.setText(
                "Queue is empty — add a gap below, or send one from the Knowledge Gaps tab."
            )
            self.queue_list.setVisible(False)
            self.queue_remove_btn.setVisible(False)
            self.queue_skip_btn.setVisible(False)
            self.queue_clear_btn.setVisible(False)
            return
        for i, gap in enumerate(queue):
            prefix = "▶ " if i == 0 else "    "
            item = QListWidgetItem(prefix + self._gap_title(gap))
            if i == 0:
                f = item.font()
                f.setBold(True)
                item.setFont(f)
            self.queue_list.addItem(item)
        n = len(queue)
        self.queue_header.setText(
            f"{n} gap{'s' if n != 1 else ''} queued — top item drives the form below"
        )
        self.queue_list.setVisible(True)
        self.queue_remove_btn.setVisible(True)
        self.queue_skip_btn.setVisible(True)
        self.queue_clear_btn.setVisible(True)

    def _on_add_gap_manually(self):
        text = self.queue_add_input.text().strip()
        if not text:
            return
        if self._main_window is None:
            showWarning(
                "Open this panel via Tools → Ankisstant so the queue is available."
            )
            return
        queue = getattr(self._main_window, "gap_queue", None)
        if queue is None:
            return
        queue.append({"title": text, "kg_id": None, "stem_html": None, "notes": None})
        self.queue_add_input.clear()
        if hasattr(self._main_window, "refresh_queue_badge"):
            self._main_window.refresh_queue_badge()
        self.refresh_queue_state(self._main_window)
        tooltip(f"Queued: {text[:60]}")

    def _on_remove_selected_gap(self):
        if self._main_window is None:
            return
        queue = getattr(self._main_window, "gap_queue", None)
        if not queue:
            return
        row = self.queue_list.currentRow()
        if row < 0 or row >= len(queue):
            tooltip("Select a gap in the list first.")
            return
        if row == 0:
            self._current_gap = None  # active gap is being removed
        removed = queue.pop(row)
        if hasattr(self._main_window, "refresh_queue_badge"):
            self._main_window.refresh_queue_badge()
        self.refresh_queue_state(self._main_window)
        tooltip(f"Removed: {self._gap_title(removed)[:60]}")

    def _on_skip_gap(self):
        if self._main_window is None:
            return
        queue = getattr(self._main_window, "gap_queue", None)
        if not queue:
            return
        skipped = queue.pop(0)
        self._current_gap = None
        if hasattr(self._main_window, "refresh_queue_badge"):
            self._main_window.refresh_queue_badge()
        self.refresh_queue_state(self._main_window)
        tooltip(f"Skipped: {self._gap_title(skipped)[:60]}")

    def _on_clear_queue(self):
        if self._main_window is None:
            return
        queue = getattr(self._main_window, "gap_queue", None)
        if not queue:
            return
        if not askUser(f"Discard all {len(queue)} queued gap(s)?"):
            return
        queue.clear()
        self._current_gap = None
        if hasattr(self._main_window, "refresh_queue_badge"):
            self._main_window.refresh_queue_badge()
        self.refresh_queue_state(self._main_window)

    # ── file attachment handlers ─────────────────────────────────────────────

    def _on_attach_files(self):
        paths, _ = QFileDialog.getOpenFileNames(
            self, "Attach PDFs / PowerPoints",
            "", "Documents (*.pdf *.pptx);;PDF (*.pdf);;PowerPoint (*.pptx);;All files (*)",
        )
        if not paths:
            return
        added = 0
        for p in paths:
            if p in self._attached_paths:
                continue
            ext = os.path.splitext(p)[1].lower()
            if ext not in (".pdf", ".pptx"):
                tooltip(f"Skipped {os.path.basename(p)} — only .pdf / .pptx supported.")
                continue
            try:
                size = os.path.getsize(p)
            except OSError as e:
                showWarning(f"Couldn't read {os.path.basename(p)}: {e}")
                continue
            mb = size / (1024 * 1024)
            if ext == ".pdf":
                if mb > 30:
                    showWarning(
                        f"{os.path.basename(p)} is {mb:.1f} MB — too large for Claude to read "
                        "in a single request (>30 MB). Split the PDF or extract relevant pages."
                    )
                    continue
                if mb > 10:
                    ok = askUser(
                        f"{os.path.basename(p)} is {mb:.1f} MB. Large PDFs can be slow "
                        "and may exhaust the model's context. Attach anyway?"
                    )
                    if not ok:
                        continue
            self._attached_paths.append(p)
            added += 1
        if added:
            self._refresh_attach_list()

    def _on_attach_clear(self):
        self._attached_paths.clear()
        self._refresh_attach_list()

    def _refresh_attach_list(self):
        self.attach_list.clear()
        for p in self._attached_paths:
            self.attach_list.addItem(QListWidgetItem(os.path.basename(p)))
        has = bool(self._attached_paths)
        self.attach_list.setVisible(has)
        self.attach_clear_btn.setVisible(has)

    def _on_generate(self):
        if not anki_utils.require_col():
            return
        mode = "source" if self.rb_source.isChecked() else "topic"
        n = self.n_cards.value()
        deck_name = self.deck.currentText().strip()
        focus = self.focus.text().strip()
        tags_raw = [t.strip() for t in self.tags.text().split(",") if t.strip()]
        audit_tag = (self.cfg.get("audit_tag") or "").strip()
        if audit_tag and audit_tag not in tags_raw:
            tags_raw.append(audit_tag)

        notetype_name = self._current_notetype_name()
        if not notetype_name or notetype_name.startswith("("):
            showWarning(
                "No notetype selected.\n\n"
                "Open Ankisstant Settings → Create with Claude and add at least "
                "one notetype profile, then pick it in the dropdown."
            )
            return
        profile = _resolved_profile(self.cfg, notetype_name)

        attachments_for_api: list[str] = []  # PDFs go through Claude (CLI Read or API doc)
        attachment_labels: list[str] = []    # for the source label

        if mode == "source":
            url = self.url.text().strip()
            text_body = self.text.toPlainText().strip()
            if url:
                fetched = None
                fetch_err: Exception | None = None
                with loading(self.go_btn, "Fetching URL…"):
                    try:
                        fetched = _fetch_url(url)
                    except (urllib.error.URLError, urllib.error.HTTPError) as e:
                        fetch_err = e
                if fetch_err is not None:
                    showWarning(f"Could not fetch URL: {fetch_err}")
                    return
                text_body = (text_body + "\n\n" + fetched).strip() if text_body else fetched

            # PPTX → in-process text extract (works on any provider).
            # PDF  → handed to Claude (CLI Read tool, or API document block).
            if self._attached_paths:
                pptx_blocks: list[str] = []
                with loading(self.go_btn, "Reading attachments…"):
                    for p in self._attached_paths:
                        ext = os.path.splitext(p)[1].lower()
                        name = os.path.basename(p)
                        if ext == ".pptx":
                            try:
                                t = _extract_pptx_text(p)
                            except Exception as e:
                                showWarning(f"Couldn't read {name}: {e}")
                                return
                            if t:
                                pptx_blocks.append(
                                    f"\n---FILE: {name}---\n{t}\n---END FILE---"
                                )
                            else:
                                showWarning(
                                    f"{name} contains no readable text.\n\n"
                                    "PowerPoint extraction only sees text on slides and "
                                    "in speaker notes — image-only slides won't work. "
                                    "Try a PDF export instead, or paste the relevant text directly."
                                )
                                return
                            attachment_labels.append(name)
                        elif ext == ".pdf":
                            attachments_for_api.append(p)
                            attachment_labels.append(name)
                if pptx_blocks:
                    text_body = (text_body + "\n\n" + "\n".join(pptx_blocks)).strip() \
                        if text_body else "\n".join(pptx_blocks)

            if not text_body and not attachments_for_api:
                showWarning("Provide pasted text, a URL, or attach a PDF / PPTX.")
                return

            # Source label: URL wins, else listed attachments, else (pasted).
            if url:
                source_label = url
            elif attachment_labels:
                source_label = ", ".join(attachment_labels)
            else:
                source_label = "(pasted text)"

            parts = [f"Generate {n} high-yield cloze cards from the SOURCE below."]
            if focus:
                parts.append(f"Focus: {focus}")
            if attachments_for_api:
                parts.append(
                    "PDF attachment(s) provided — use them as the primary source."
                )
            if text_body:
                parts.append(f"\n---SOURCE---\n{text_body}\n---END SOURCE---")
            user_msg = "\n".join(parts)
            topic_label = None
        else:
            topic_label = self.topic.text().strip()
            if not topic_label:
                showWarning("Type a topic.")
                return
            source_label = None
            user_msg = (
                f"Generate {n} high-yield cloze cards on the TOPIC: {topic_label}\n"
                + (f"Focus: {focus}\n" if focus else "")
            )

        model = self.cfg.get("model") or None
        loading_label = (
            "Asking Claude (reading attachments)…" if attachments_for_api
            else "Asking Claude…"
        )
        skill_invocation, skill_id = _resolve_skill(profile)
        system_prompt = _augment_system(CARD_GEN_SYSTEM, profile)
        with loading(self.go_btn, loading_label):
            cards = core_api.ask_claude_json(
                prompt=user_msg, system=system_prompt, max_tokens=4096, model=model,
                skill_id=skill_id, skill_invocation=skill_invocation,
                attachments=attachments_for_api or None,
            )

        if not isinstance(cards, list) or not all(
            isinstance(c, dict) and "front" in c and "extra" in c for c in cards
        ):
            return  # ask_claude_json already showed a tooltip

        self.cfg["default_deck"] = deck_name
        self.cfg["default_n_cards"] = n
        self.cfg["selected_notetype"] = notetype_name
        save_tool_config("card_creator", self.cfg)

        dlg = ReviewDialog(
            cards=cards, mode=mode,
            source_label=source_label, topic_label=topic_label, focus=focus,
            deck_name=deck_name, tags=tags_raw,
            profile=profile, panel_image_paths=list(self._extra_image_paths),
            parent=self,
        )
        result = dlg.exec()

        # LO-gap queue: only pop on a successful acceptance — Cancel/reject
        # leaves the gap in place so the user can retry it.
        try:
            accepted = result == QDialog.DialogCode.Accepted
        except Exception:
            accepted = bool(result)
        if (
            accepted
            and self._current_gap is not None
            and self._main_window is not None
            and getattr(self._main_window, "gap_queue", None)
            and self._main_window.gap_queue
            and self._main_window.gap_queue[0] == self._current_gap
        ):
            popped = self._main_window.gap_queue.pop(0)
            # If this gap traces back to a KG, mark it done in the store and
            # nudge the KG panel to refresh.
            try:
                kg_id = popped.get("kg_id") if isinstance(popped, dict) else None
                if kg_id:
                    from .kg import store as kg_store
                    kg_store.update(kg_id, status="done")
                    from . import knowledge_gaps
                    knowledge_gaps._refresh_open_panel()
            except Exception as e:
                print(f"[ankisstant] mark KG done failed: {e}")
            self._current_gap = None
            if hasattr(self._main_window, "refresh_queue_badge"):
                self._main_window.refresh_queue_badge()
            self.refresh_queue_state(self._main_window)


# ── review dialog (separate window) ──────────────────────────────────────────

class _CardRow(QFrame):
    APPROVED, REJECTED, PENDING = "approved", "rejected", "pending"

    def __init__(self, idx: int, card: dict, dup_msg: str | None,
                 on_state_change, on_regenerate, on_split, on_one_by_one,
                 parent=None):
        super().__init__(parent)
        self.idx = idx
        self.card = card
        self.state = self.APPROVED
        self._on_state_change = on_state_change
        self._on_regenerate = on_regenerate
        self._on_split = on_split
        self._on_one_by_one = on_one_by_one

        self.setFrameShape(QFrame.Shape.StyledPanel)
        self.setStyleSheet("QFrame { border: 1px solid #ccc; border-radius: 4px; padding: 6px; }")

        v = QVBoxLayout(self)

        header = QHBoxLayout()
        self.header_label = QLabel()
        self.header_label.setStyleSheet("font-weight: 600;")
        header.addWidget(self.header_label)
        header.addStretch(1)
        # Cards start included by default — single toggle button handles reject/undo.
        self.reject_btn = QPushButton()
        self.reject_btn.clicked.connect(self._toggle_reject)
        self.split_btn = QPushButton("✂️ Split")
        self.split_btn.setToolTip("Break this card into multiple atomic single-cloze cards.")
        self.split_btn.clicked.connect(self._on_split_clicked)
        self.obo_btn = QPushButton("1️⃣ One-by-one")
        self.obo_btn.setToolTip(
            "Rewrite as a single card with sibling {{c1}} clozes that AnKing reveals "
            "sequentially. Sets the 'One by one' field to y."
        )
        self.obo_btn.clicked.connect(self._on_obo_clicked)
        self.img_btn = QPushButton("📷 Image")
        self.img_btn.setToolTip(
            "Attach images to JUST this card's Extra field (e.g. a specific flowchart). "
            "Panel-level images are still applied to every card."
        )
        self.img_btn.clicked.connect(self._on_attach_image)
        for b in (self.img_btn, self.reject_btn, self.split_btn, self.obo_btn):
            header.addWidget(b)
        v.addLayout(header)

        if dup_msg:
            dup = QLabel(f"⚠ {dup_msg}")
            dup.setStyleSheet("color: #b85c00; font-size: 11px;")
            dup.setWordWrap(True)
            v.addWidget(dup)

        self.front_lbl = QLabel(card.get("front", ""))
        self.front_lbl.setTextFormat(Qt.TextFormat.RichText)
        self.front_lbl.setWordWrap(True)
        v.addWidget(self.front_lbl)

        self.extra_toggle = QPushButton("Show extra ▾")
        self.extra_toggle.setCheckable(True)
        self.extra_toggle.setFlat(True)
        self.extra_toggle.setStyleSheet("text-align: left; padding: 2px;")
        self.extra_toggle.clicked.connect(self._toggle_extra)
        v.addWidget(self.extra_toggle)

        self.extra_lbl = QLabel(card.get("extra", ""))
        self.extra_lbl.setTextFormat(Qt.TextFormat.RichText)
        self.extra_lbl.setWordWrap(True)
        self.extra_lbl.setStyleSheet("color: #555; padding-left: 12px;")
        self.extra_lbl.setVisible(False)
        v.addWidget(self.extra_lbl)

        hint_row = QHBoxLayout()
        hint_row.addWidget(QLabel("Regen hint:"))
        self.hint_input = QLineEdit()
        self.hint_input.setPlaceholderText(
            "(optional) e.g. shorter, focus on mechanism, use simpler wording…"
        )
        self.hint_input.returnPressed.connect(self._on_regen_clicked)
        hint_row.addWidget(self.hint_input, 1)
        self.regen_btn = QPushButton("🔄 Regenerate")
        self.regen_btn.clicked.connect(self._on_regen_clicked)
        hint_row.addWidget(self.regen_btn)
        v.addLayout(hint_row)

        self._refresh_header()

    def _toggle_extra(self):
        on = self.extra_toggle.isChecked()
        self.extra_lbl.setVisible(on)
        self.extra_toggle.setText("Hide extra ▴" if on else "Show extra ▾")

    def _set_state(self, state):
        self.state = state
        self._refresh_header()
        self._on_state_change()

    def _toggle_reject(self):
        self._set_state(self.APPROVED if self.state == self.REJECTED else self.REJECTED)

    def _refresh_header(self):
        suffix = ""
        n_imgs = len(self.card.get("_image_paths") or [])
        if n_imgs:
            suffix = f"  · 📷 {n_imgs}"
        if self.state == self.REJECTED:
            self.header_label.setText(f"Card {self.idx + 1} — ✗ rejected{suffix}")
            self.reject_btn.setText("↺ Re-include")
        else:
            self.header_label.setText(f"Card {self.idx + 1} — ✓ included{suffix}")
            self.reject_btn.setText("✗ Reject")

    def _on_regen_clicked(self):
        hint = self.hint_input.text().strip()
        with loading(self.regen_btn, "Regenerating…"):
            new = self._on_regenerate(self.card, hint)
        if new is None:
            return
        self.card = new
        self.front_lbl.setText(new.get("front", ""))
        self.extra_lbl.setText(new.get("extra", ""))
        self.hint_input.clear()
        self._set_state(self.APPROVED)

    def _on_split_clicked(self):
        with loading(self.split_btn, "Splitting…"):
            self._on_split(self)

    def _on_obo_clicked(self):
        with loading(self.obo_btn, "Rewriting…"):
            new = self._on_one_by_one(self.card)
        if new is None:
            return
        new["one_by_one"] = True
        self.card = new
        self.front_lbl.setText(new.get("front", ""))
        self.extra_lbl.setText(new.get("extra", ""))
        self.header_label.setText(
            f"{self.header_label.text()}  · 1️⃣ one-by-one"
        )
        self._set_state(self.APPROVED)

    def _on_attach_image(self):
        paths, _ = QFileDialog.getOpenFileNames(
            self, "Attach images to this card",
            "", "Images (*.png *.jpg *.jpeg *.gif *.webp *.bmp *.svg);;All files (*)",
        )
        if not paths:
            return
        imgs = list(self.card.get("_image_paths") or [])
        for p in paths:
            if p not in imgs:
                imgs.append(p)
        self.card["_image_paths"] = imgs
        self._refresh_header()


class ReviewDialog(QDialog):
    def __init__(self, cards, mode, source_label, topic_label, focus,
                 deck_name, tags, profile=None, panel_image_paths=None,
                 parent=None):
        super().__init__(parent)
        self.cfg = tool_config("card_creator")
        self.cards_in = cards
        self.mode = mode
        self.source_label = source_label
        self.topic_label = topic_label
        self.focus = focus
        self.deck_name = deck_name
        self.tags = tags
        # Selected notetype profile + panel-level image attachments. Falls
        # back to a synthesised legacy-fields profile when called from older
        # code paths that don't pass `profile`.
        self.profile = profile or _resolved_profile(
            self.cfg, (self.cfg.get("selected_notetype") or self.cfg.get("default_notetype") or "").strip()
        )
        self.panel_image_paths: list[str] = list(panel_image_paths or [])
        self.rows: list[_CardRow] = []
        self.setWindowTitle("Review proposed cards")
        self.setMinimumSize(800, 700)
        self._build()

    def _build(self):
        root = QVBoxLayout(self)

        self.header = QLabel()
        self.header.setStyleSheet("font-weight: 600;")
        root.addWidget(self.header)

        meta = QLabel(self._meta_text())
        meta.setStyleSheet("color: gray; font-size: 11px;")
        meta.setWordWrap(True)
        root.addWidget(meta)

        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        inner = QWidget()
        self.inner_layout = QVBoxLayout(inner)
        self.inner_layout.setSpacing(8)
        scroll.setWidget(inner)
        root.addWidget(scroll, 1)

        for i, card in enumerate(self.cards_in):
            dup = self._dup_check(card)
            row = _CardRow(i, card, dup, self._on_state_change,
                           self._on_regenerate, self._on_split,
                           self._on_one_by_one)
            self.rows.append(row)
            self.inner_layout.addWidget(row)
        self.inner_layout.addStretch(1)

        bulk = QHBoxLayout()
        ba = QPushButton("Approve all")
        ba.clicked.connect(lambda: self._bulk(_CardRow.APPROVED))
        br = QPushButton("Reject all")
        br.clicked.connect(lambda: self._bulk(_CardRow.REJECTED))
        bulk.addWidget(ba)
        bulk.addWidget(br)
        bulk.addStretch(1)
        root.addLayout(bulk)

        bb = QHBoxLayout()
        self.create_btn = QPushButton("Create All (trust mode)")
        self.create_btn.clicked.connect(self._on_create_all)
        self.add_screen_btn = QPushButton("Open in Add Screen")
        self.add_screen_btn.clicked.connect(self._on_add_screen)
        cancel = QPushButton("Cancel")
        cancel.clicked.connect(self.reject)
        bb.addStretch(1)
        bb.addWidget(cancel)
        bb.addWidget(self.add_screen_btn)
        bb.addWidget(self.create_btn)
        root.addLayout(bb)

        self._refresh_header()

    def _meta_text(self):
        parts = [f"Deck: {self.deck_name}",
                 f"Notetype: {self.profile.get('name', '')}",
                 f"Tags: {', '.join(self.tags)}"]
        if self.panel_image_paths:
            n = len(self.panel_image_paths)
            parts.append(f"Extra image{'s' if n != 1 else ''}: {n} (applied to every card)")
        if self.mode == "source":
            parts.append(f"Source: {self.source_label or '(pasted)'}")
        else:
            parts.append(f"Topic: {self.topic_label}")
        if self.focus:
            parts.append(f"Focus: {self.focus}")
        return " · ".join(parts)

    def _bulk(self, state):
        for r in self.rows:
            r.state = state
            r._refresh_header()
        self._on_state_change()

    def _on_state_change(self):
        self._refresh_header()

    def _refresh_header(self):
        approved = sum(1 for r in self.rows if r.state == _CardRow.APPROVED)
        rejected = sum(1 for r in self.rows if r.state == _CardRow.REJECTED)
        self.header.setText(
            f"{approved} approved · {rejected} rejected · {len(self.rows)} total"
        )

    # ── duplicate detection ───────────────────────────────────────────────────

    def _dup_check(self, card) -> str | None:
        plain = anki_utils.strip_cloze(anki_utils.strip_html(card.get("front", "")))[:50].strip()
        if len(plain) < 12:
            return None
        safe = plain.replace("\\", "\\\\").replace('"', '\\"')
        front_field = self.profile.get("front_field", "Text")
        try:
            nids = mw.col.find_notes(f'"{front_field}:*{safe}*"')
        except Exception:
            return None
        if nids:
            return f"Possible duplicate — {len(nids)} note(s) already contain '{plain[:40]}…'"
        return None

    # ── regenerate / one-by-one / split ───────────────────────────────────────

    def _on_regenerate(self, old_card, hint: str = ""):
        if hint:
            instruction = (
                f"Regenerate ONE card on the same subject, applying this user "
                f"feedback: {hint}"
            )
        else:
            instruction = (
                "Regenerate ONE card on the same subject — different angle or wording."
            )
        prompt = (
            "Original card front:\n" + old_card.get("front", "") + "\n\n"
            "Original card extra:\n" + old_card.get("extra", "") + "\n\n"
            + instruction + " "
            + (f"Focus: {self.focus}" if self.focus else "")
        )
        skill_invocation, skill_id = _resolve_skill(self.profile)
        new = core_api.ask_claude_json(
            prompt=prompt,
            system=_augment_system(SINGLE_REGEN_SYSTEM, self.profile),
            max_tokens=1024, model=self.cfg.get("model") or None,
            skill_id=skill_id, skill_invocation=skill_invocation,
        )
        if not isinstance(new, dict) or "front" not in new or "extra" not in new:
            return None
        # Preserve any per-card images the user attached pre-regen.
        if old_card.get("_image_paths"):
            new["_image_paths"] = list(old_card["_image_paths"])
        return new

    def _on_one_by_one(self, old_card):
        prompt = (
            "Original card front:\n" + old_card.get("front", "") + "\n\n"
            "Original card extra:\n" + old_card.get("extra", "") + "\n\n"
            "Rewrite as ONE card with multiple sibling {{c1::...}} clozes so AnKing's "
            "'One by one' addon reveals each in sequence."
        )
        skill_invocation, skill_id = _resolve_skill(self.profile)
        new = core_api.ask_claude_json(
            prompt=prompt,
            system=_augment_system(ONE_BY_ONE_SYSTEM, self.profile),
            max_tokens=1024, model=self.cfg.get("model") or None,
            skill_id=skill_id, skill_invocation=skill_invocation,
        )
        if not isinstance(new, dict) or "front" not in new or "extra" not in new:
            return None
        front = new["front"]
        if re.search(r"\{\{c[2-9]\d*::", front):
            new["front"] = re.sub(r"\{\{c\d+::", "{{c1::", front)
        if old_card.get("_image_paths"):
            new["_image_paths"] = list(old_card["_image_paths"])
        return new

    def _on_split(self, row):
        prompt = (
            "Original card front:\n" + row.card.get("front", "") + "\n\n"
            "Original card extra:\n" + row.card.get("extra", "") + "\n\n"
            "Split this card into the smallest number of ATOMIC single-cloze cards "
            "that preserve all the testable facts. Each output card must have exactly "
            "one {{c1::...}} cloze."
        )
        skill_invocation, skill_id = _resolve_skill(self.profile)
        new_cards = core_api.ask_claude_json(
            prompt=prompt,
            system=_augment_system(SPLIT_SYSTEM, self.profile),
            max_tokens=2048, model=self.cfg.get("model") or None,
            skill_id=skill_id, skill_invocation=skill_invocation,
        )
        if (not isinstance(new_cards, list) or not new_cards
                or not all(isinstance(c, dict) and "front" in c and "extra" in c for c in new_cards)):
            return

        idx_in_layout = self.inner_layout.indexOf(row)
        idx_in_rows = self.rows.index(row)
        self.inner_layout.removeWidget(row)
        row.setParent(None)
        row.deleteLater()
        self.rows.pop(idx_in_rows)

        for offset, card in enumerate(new_cards):
            dup = self._dup_check(card)
            new_row = _CardRow(
                idx=0, card=card, dup_msg=dup,
                on_state_change=self._on_state_change,
                on_regenerate=self._on_regenerate,
                on_split=self._on_split,
                on_one_by_one=self._on_one_by_one,
            )
            self.inner_layout.insertWidget(idx_in_layout + offset, new_row)
            self.rows.insert(idx_in_rows + offset, new_row)

        for new_idx, r in enumerate(self.rows):
            r.idx = new_idx
            r._refresh_header()
        self._on_state_change()

    # ── confirm paths ─────────────────────────────────────────────────────────

    def _approved_cards(self):
        return [r.card for r in self.rows if r.state == _CardRow.APPROVED]

    def _enrich_extra(self, extra: str) -> str:
        out = extra or ""
        if self.mode == "source" and self.source_label:
            cite = f"Source: {self.source_label}"
            if cite not in out:
                out = (out + "<br>" if out else "") + cite
        return out

    def _images_for(self, card) -> str:
        """Build an <img>-tag fragment combining panel-level images and any
        per-card images attached during review. Files are copied into Anki
        media here (NOT at generation time) so we only import what actually
        gets created."""
        paths: list[str] = []
        paths.extend(self.panel_image_paths)
        paths.extend(card.get("_image_paths") or [])
        if not paths:
            return ""
        return _img_tags_for(paths)

    def _get_notetype(self):
        name = (self.profile.get("name") or "").strip()
        if not name:
            showWarning(
                "No notetype configured.\n\n"
                "Open Ankisstant Settings → Create with Claude and add a "
                "notetype profile."
            )
            return None
        nt = mw.col.models.by_name(name)
        if nt is None:
            showWarning(
                f"Notetype not found: {name}\n\n"
                "Edit or remove this notetype profile under Settings → "
                "Create with Claude."
            )
            return None
        front_field = (self.profile.get("front_field") or "Text").strip()
        field_names = {f["name"] for f in nt.get("flds", [])}
        if front_field and front_field not in field_names:
            showWarning(
                f"Notetype '{name}' has no field named '{front_field}'.\n\n"
                f"Available fields: {', '.join(sorted(field_names)) or '(none)'}\n\n"
                "Update the profile's 'Front field' under Settings → Create with Claude."
            )
            return None
        # Cloze sanity check — non-cloze notetypes will silently swallow {{c1::…}}.
        if nt.get("type", 0) != 1:
            log.warn(f"notetype {name!r} is not a cloze type — generated cards may not render as expected.")
        return nt

    def _get_deck_id(self):
        return mw.col.decks.id(self.deck_name)

    def _set_fields(self, note, card):
        front_field   = self.profile.get("front_field", "Text")
        extra_field   = self.profile.get("extra_field", "Extra")
        image_field   = self.profile.get("image_field", extra_field)
        sources_field = (self.profile.get("sources_field") or "").strip()
        obo_field     = self.profile.get("one_by_one_field", "One by one")
        try:
            note[front_field] = card.get("front", "")
        except KeyError:
            note.fields[0] = card.get("front", "")
        if extra_field in note:
            note[extra_field] = self._enrich_extra(card.get("extra", ""))
        # Images go into image_field (often == extra_field, in which case we
        # append; if it's a separate field, that field is set directly).
        img_html = self._images_for(card)
        if img_html and image_field in note:
            if image_field == extra_field:
                existing = note[image_field] or ""
                note[image_field] = (existing + "<br>" if existing else "") + img_html
            else:
                note[image_field] = img_html
        source_html = (card.get("source") or "").strip()
        if source_html and sources_field and sources_field in note:
            if sources_field == extra_field:
                existing = note[sources_field] or ""
                note[sources_field] = (existing + "<br>" if existing else "") + source_html
            else:
                note[sources_field] = source_html
        if card.get("one_by_one") and obo_field in note:
            note[obo_field] = "y"
        for t in self.tags:
            note.add_tag(t)

    def _on_create_all(self):
        if not anki_utils.require_col():
            return
        approved = self._approved_cards()
        if not approved:
            tooltip("No approved cards.")
            return
        if not askUser(f"Create {len(approved)} card(s) directly in deck '{self.deck_name}'?"):
            return
        nt = self._get_notetype()
        if nt is None:
            return
        deck_id = self._get_deck_id()
        created_ids = []
        if hasattr(mw, "checkpoint"):
            mw.checkpoint("Create with Claude")
        for card in approved:
            note = mw.col.new_note(nt)
            self._set_fields(note, card)
            mw.col.add_note(note, deck_id)
            created_ids.append(note.id)
        mw.reset()
        _append_session(
            mode=self.mode, source=self.source_label, topic=self.topic_label,
            cards_proposed=len(self.cards_in), cards_created=len(created_ids),
            card_ids=created_ids,
        )
        tooltip(f"Created {len(created_ids)} card(s).")
        self.accept()

    def _on_add_screen(self):
        if not anki_utils.require_col():
            return
        approved = self._approved_cards()
        if not approved:
            tooltip("No approved cards.")
            return
        nt = self._get_notetype()
        if nt is None:
            return
        deck_id = self._get_deck_id()

        ctx = {
            "nt": nt,
            "deck_id": deck_id,
            "tags": list(self.tags),
            "front_field": self.profile.get("front_field", "Text"),
            "extra_field": self.profile.get("extra_field", "Extra"),
            "image_field":   self.profile.get("image_field", self.profile.get("extra_field", "Extra")),
            "sources_field": (self.profile.get("sources_field") or "").strip(),
            "obo_field":     self.profile.get("one_by_one_field", "One by one"),
            "enricher":      self._enrich_extra,
            "images_for":    self._images_for,
        }
        # First card now, rest auto-loaded after each Anki "Add" click.
        _start_add_queue(approved, ctx)

        _append_session(
            mode=self.mode, source=self.source_label, topic=self.topic_label,
            cards_proposed=len(self.cards_in), cards_created=0, card_ids=[],
        )
        self.accept()


# ── persistent Add-Screen queue (survives ReviewDialog dismissal) ────────────

_add_queue: list[dict] = []
_add_queue_ctx: dict | None = None
_add_dialog = None  # the AddCards instance we're driving
_hook_attached = False


def _fill_addcards(ac, card: dict, ctx: dict) -> None:
    nt = ctx["nt"]
    new_note = mw.col.new_note(nt)
    front_field = ctx["front_field"]
    extra_field = ctx["extra_field"]
    image_field = ctx.get("image_field", extra_field)
    obo_field   = ctx["obo_field"]
    try:
        new_note[front_field] = card.get("front", "")
    except KeyError:
        new_note.fields[0] = card.get("front", "")
    if extra_field in new_note:
        new_note[extra_field] = ctx["enricher"](card.get("extra", ""))
    img_html = ctx["images_for"](card) if ctx.get("images_for") else ""
    if img_html and image_field in new_note:
        if image_field == extra_field:
            existing = new_note[image_field] or ""
            new_note[image_field] = (existing + "<br>" if existing else "") + img_html
        else:
            new_note[image_field] = img_html
    sources_field = ctx.get("sources_field") or ""
    source_html = (card.get("source") or "").strip()
    if source_html and sources_field and sources_field in new_note:
        if sources_field == extra_field:
            existing = new_note[sources_field] or ""
            new_note[sources_field] = (existing + "<br>" if existing else "") + source_html
        else:
            new_note[sources_field] = source_html
    if card.get("one_by_one") and obo_field in new_note:
        new_note[obo_field] = "y"
    for t in ctx["tags"]:
        new_note.add_tag(t)
    try:
        ac.set_note(new_note, deck_id=ctx["deck_id"])
    except Exception as e:
        print(f"[ankisstant] ac.set_note failed: {e}; falling back to editor.set_note")
        ac.editor.set_note(new_note)
    # Defensive: re-apply tags on the editor in case sticky-tag behaviour clobbered them.
    try:
        if hasattr(ac.editor, "tags") and hasattr(ac.editor.tags, "setText"):
            ac.editor.tags.setText(" ".join(new_note.tags))
    except Exception as e:
        print(f"[ankisstant] couldn't refresh editor tag widget: {e}")


def _start_add_queue(cards: list[dict], ctx: dict) -> None:
    global _add_queue, _add_queue_ctx, _add_dialog
    if not cards:
        return
    mw.col.models.set_current(ctx["nt"])
    mw.col.decks.select(ctx["deck_id"])

    from aqt import dialogs
    ac = dialogs.open("AddCards", mw)
    _add_dialog = ac
    _add_queue = list(cards[1:])
    _add_queue_ctx = ctx

    _fill_addcards(ac, cards[0], ctx)

    if _add_queue:
        _attach_add_hook()
        try:
            if not getattr(ac, "_ankisstant_finished_hooked", False):
                ac.finished.connect(_on_add_dialog_finished)
                ac._ankisstant_finished_hooked = True
        except Exception as e:
            print(f"[ankisstant] couldn't hook AddCards.finished: {e}")
        tooltip(
            f"{len(_add_queue) + 1} card(s) queued. Click Anki's Add button — "
            f"the next will load automatically. Close the window to discard the rest."
        )
    else:
        tooltip("Card sent to Add Cards. Review then click Add.")


def _advance_add_queue():
    global _add_queue, _add_queue_ctx, _add_dialog
    if not _add_queue or _add_queue_ctx is None or _add_dialog is None:
        _stop_add_queue()
        return
    ac = _add_dialog
    next_card = _add_queue.pop(0)
    _fill_addcards(ac, next_card, _add_queue_ctx)
    remaining = len(_add_queue)
    if remaining:
        tooltip(f"{remaining} card(s) left in queue.")
    else:
        tooltip("Last queued card loaded. Click Add to finish.")


def _on_add_did_add_note(note):
    # Defer so Anki's own post-Add reset (which blanks the editor) finishes first.
    QTimer.singleShot(0, _advance_add_queue)


def _on_add_dialog_finished(*_args):
    _stop_add_queue()


def _attach_add_hook():
    global _hook_attached
    if _hook_attached:
        return
    gui_hooks.add_cards_did_add_note.append(_on_add_did_add_note)
    _hook_attached = True


def _stop_add_queue():
    global _add_queue, _add_queue_ctx, _add_dialog, _hook_attached
    if _add_queue:
        tooltip(f"Discarded {len(_add_queue)} unsent card(s) from the queue.")
    _add_queue = []
    _add_queue_ctx = None
    _add_dialog = None
    if _hook_attached:
        try:
            gui_hooks.add_cards_did_add_note.remove(_on_add_did_add_note)
        except ValueError:
            pass
        _hook_attached = False


# ── Tool contract ────────────────────────────────────────────────────────────

_panel: CreatorPanel | None = None


def init(main_window) -> None:
    return None


_scroll = None


def get_panel():
    global _panel, _scroll
    if _panel is None:
        from aqt.qt import QScrollArea
        _panel = CreatorPanel()
        _scroll = QScrollArea()
        _scroll.setWidgetResizable(True)
        _scroll.setFrameShape(QScrollArea.Shape.NoFrame)
        _scroll.setWidget(_panel)
    else:
        _panel.cfg = tool_config("card_creator")
        _panel._rebuild_notetype_combo()
    _panel.refresh_setup_banner()
    return _scroll
