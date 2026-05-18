# Analyse Knowledge Gaps — paste an LO + pick an Anki tag, Claude reads every
# card under that tag and reports specific knowledge gaps. Approved gaps flow
# into the Create with Claude queue.
#
# Layered on top of existing tools; does not modify Browse or Create logic.

from __future__ import annotations

from aqt import mw
from aqt.qt import (
    QGroupBox, QHBoxLayout, QLabel, QLineEdit, QListWidget, QListWidgetItem,
    QPlainTextEdit, QPushButton, Qt, QVBoxLayout, QWidget,
)
from aqt.utils import askUser, showWarning, tooltip

from ..core import anki_utils, api as core_api
from ..core.config import tool_config, save_tool_config
from ..core.qt_utils import (
    attach_tag_completer, loading, make_help_button, make_setup_banner,
    provider_configured,
)
from .kg import store as kg_store


NAME = "Analyse Knowledge Gaps"


GAP_SYSTEM_TMPL = (
    "You audit whether a medical student's Anki cards cover a learning objective.\n\n"
    "You will receive:\n"
    "- A learning objective (LO)\n"
    "- The front-of-card text for every card the student has tagged for that LO\n\n"
    "Identify SPECIFIC parts of the LO that are NOT addressed by any of the cards.\n\n"
    "RULES:\n"
    "- Each gap is one short, SPECIFIC statement (under 14 words) — not a broad topic.\n"
    "- Phrase each gap as a testable knowledge unit (a fact, mechanism, "
    "distinction, criterion, risk, complication).\n"
    "- Do NOT list things the cards already cover, even partially.\n"
    "- Return 0–{max_n} gaps. Quality over quantity. If the cards cover the LO "
    "well, return [].\n\n"
    "Return ONLY a JSON array of strings. No prose, no markdown fences."
)


def _front_preview(note, front_field: str, limit: int = 400) -> str:
    fld = note[front_field] if front_field in note else note.fields[0]
    text = anki_utils.strip_html(fld)
    if len(text) > limit:
        text = text[:limit].rstrip() + "…"
    return text


def _build_tag_query(tag: str, notetype_filter: str) -> str:
    parts = [f'"tag:{tag}"']
    if notetype_filter:
        parts.append(f'"note:*{notetype_filter}*"')
    return " ".join(parts)


class GapAnalyserPanel(QWidget):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.cfg = tool_config("gap_analyser")
        self._build()

    def _build(self):
        root = QVBoxLayout(self)
        root.setContentsMargins(16, 16, 16, 16)
        root.setSpacing(8)

        self._setup_banner = make_setup_banner(self)
        root.addWidget(self._setup_banner)
        self.refresh_setup_banner()

        title_row = QHBoxLayout()
        title = QLabel("<h2 style='margin:0'>Analyse Knowledge Gaps</h2>")
        title.setTextFormat(Qt.TextFormat.RichText)
        title_row.addWidget(title)
        title_row.addStretch(1)
        title_row.addWidget(make_help_button(
            "Analyse Knowledge Gaps — help",
            "<h3>What it does</h3>"
            "<p>Pastes one of your LOs and an Anki tag. Claude reads the "
            "<i>fronts</i> of every card under that tag and flags specific "
            "concepts it can't see covered.</p>"
            "<h3>Workflow</h3>"
            "<ol>"
            "<li>Paste an LO into the top box.</li>"
            "<li>Type or pick the tag holding the cards for that LO. "
            "Click <b>Count cards</b> to see how many you'll send.</li>"
            "<li>Click <b>Analyse</b>. Tick the gaps you want to keep, then "
            "<b>Send to Knowledge Gaps</b>. They land in the unified KG queue "
            "where you can route each one to Browse or Create.</li>"
            "</ol>"
            "<h3>Settings</h3>"
            "<p>Card cap, notetype filter, and which field counts as the "
            "'front' live in <b>Ankisstant Settings → Analyse Knowledge Gaps</b>.</p>",
            self,
        ))
        root.addLayout(title_row)

        intro = QLabel(
            "Paste a learning objective, pick the Anki tag holding cards for that LO, "
            "and Claude will flag specific gaps. Approved gaps land in the "
            "<b>Knowledge Gaps</b> queue — send each to Browse or Create from there."
        )
        intro.setTextFormat(Qt.TextFormat.RichText)
        intro.setWordWrap(True)
        intro.setStyleSheet("color: gray; font-size: 11px;")
        root.addWidget(intro)

        # LO input
        root.addWidget(QLabel("Learning Objective:"))
        self.lo_input = QPlainTextEdit()
        self.lo_input.setPlaceholderText(
            "Paste the LO here — e.g. 'Describe the pharmacology, indications and "
            "adverse effects of beta-blockers.'"
        )
        self.lo_input.setMinimumHeight(110)
        self.lo_input.setMinimumWidth(500)
        self.lo_input.textChanged.connect(self._update_button_state)
        root.addWidget(self.lo_input)

        # Tag input row
        tag_row = QHBoxLayout()
        tag_row.addWidget(QLabel("Tag:"))
        self.tag_input = QLineEdit(self.cfg.get("last_used_tag", ""))
        self.tag_input.setMinimumWidth(420)
        self.tag_input.setPlaceholderText("e.g. School::Year3::Cardio::BetaBlockers")
        self.tag_input.textChanged.connect(self._update_button_state)
        attach_tag_completer(self.tag_input, multi=False)
        tag_row.addWidget(self.tag_input, 1)
        self.count_btn = QPushButton("Count cards")
        self.count_btn.setAutoDefault(False)
        self.count_btn.clicked.connect(self._on_count)
        tag_row.addWidget(self.count_btn)
        root.addLayout(tag_row)

        self.status = QLabel("")
        self.status.setStyleSheet("color: gray; font-size: 11px;")
        self.status.setWordWrap(True)
        root.addWidget(self.status)

        # Analyse button
        btn_row = QHBoxLayout()
        btn_row.addStretch(1)
        self.analyse_btn = QPushButton("🔍 Analyse Gaps with Claude")
        self.analyse_btn.setEnabled(False)
        self.analyse_btn.clicked.connect(self._on_analyse)
        btn_row.addWidget(self.analyse_btn)
        root.addLayout(btn_row)

        # Gaps panel (hidden until results)
        self.gaps_box = QGroupBox("Suggested gaps")
        gv = QVBoxLayout(self.gaps_box)
        gv.setSpacing(6)
        hint = QLabel(
            "Tick the gaps you want to keep, then send them to the Knowledge "
            "Gaps queue. From there each one can be routed to Browse or Create."
        )
        hint.setStyleSheet("color: gray; font-size: 11px;")
        hint.setWordWrap(True)
        gv.addWidget(hint)
        self.gaps_list = QListWidget()
        self.gaps_list.setAlternatingRowColors(True)
        gv.addWidget(self.gaps_list, 1)

        select_row = QHBoxLayout()
        sel_all = QPushButton("Select all")
        sel_all.setAutoDefault(False)
        sel_all.clicked.connect(self._select_all)
        sel_none = QPushButton("Select none")
        sel_none.setAutoDefault(False)
        sel_none.clicked.connect(self._select_none)
        select_row.addWidget(sel_all)
        select_row.addWidget(sel_none)
        select_row.addStretch(1)
        self.dismiss_btn = QPushButton("Dismiss")
        self.dismiss_btn.setAutoDefault(False)
        self.dismiss_btn.clicked.connect(self._dismiss)
        self.queue_btn = QPushButton("Send Approved Gaps to Knowledge Gaps →")
        self.queue_btn.setAutoDefault(False)
        self.queue_btn.clicked.connect(self._on_queue)
        select_row.addWidget(self.dismiss_btn)
        select_row.addWidget(self.queue_btn)
        gv.addLayout(select_row)
        self.gaps_box.setVisible(False)
        root.addWidget(self.gaps_box, 1)

    # ── enable/disable ───────────────────────────────────────────────────────

    def refresh_setup_banner(self) -> None:
        try:
            self._setup_banner.setVisible(not provider_configured())
        except Exception:
            pass

    def _update_button_state(self, *_):
        lo_ok = bool(self.lo_input.toPlainText().strip())
        tag_ok = bool(self.tag_input.text().strip())
        self.analyse_btn.setEnabled(lo_ok and tag_ok)

    # ── tag count preview ────────────────────────────────────────────────────

    def _on_count(self):
        if not anki_utils.require_col():
            return
        tag = self.tag_input.text().strip()
        if not tag:
            tooltip("Enter a tag first.")
            return
        notetype_filter = (self.cfg.get("notetype_filter") or "").strip()
        try:
            nids = list(mw.col.find_notes(_build_tag_query(tag, notetype_filter)))
        except Exception as e:
            showWarning(f"Couldn't search tag: {e}")
            return
        n = len(nids)
        if n == 0:
            self.status.setText(f"No notes found under tag '{tag}'.")
        else:
            self.status.setText(
                f"{n} note(s) under tag '{tag}'"
                + (f" (notetype filter: {notetype_filter})" if notetype_filter else "")
            )

    # ── analyse ──────────────────────────────────────────────────────────────

    def _on_analyse(self):
        if not anki_utils.require_col():
            return
        lo = self.lo_input.toPlainText().strip()
        tag = self.tag_input.text().strip()
        if not lo or not tag:
            return

        notetype_filter = (self.cfg.get("notetype_filter") or "").strip()
        front_field = self.cfg.get("front_field", "Text")
        max_cards = int(self.cfg.get("max_cards", 80))

        try:
            nids = list(mw.col.find_notes(_build_tag_query(tag, notetype_filter)))
        except Exception as e:
            showWarning(f"Couldn't search tag: {e}")
            return
        if not nids:
            self.status.setText(f"No notes found under tag '{tag}' — nothing to analyse.")
            return

        if len(nids) > max_cards:
            ok = askUser(
                f"{len(nids)} cards under '{tag}' — that's more than the cap "
                f"({max_cards}). Only the first {max_cards} will be sent to Claude. "
                f"Continue?"
            )
            if not ok:
                return
            nids = nids[:max_cards]

        card_fronts: list[str] = []
        for nid in nids:
            try:
                note = mw.col.get_note(nid)
            except Exception:
                continue
            preview = _front_preview(note, front_field)
            if preview:
                card_fronts.append(preview)
        if not card_fronts:
            showWarning("Couldn't read the front field on any card under that tag.")
            return

        max_n = int(self.cfg.get("max_gaps", 10))
        system = GAP_SYSTEM_TMPL.format(max_n=max_n)
        user_msg = (
            "Learning objective:\n" + lo + "\n\n"
            f"Cards currently tagged for this LO ({len(card_fronts)}):\n"
            + "\n".join(f"- {c}" for c in card_fronts)
        )

        self.status.setText(
            f"Asking Claude what's missing from {len(card_fronts)} card(s) under '{tag}'…"
        )
        model = self.cfg.get("model") or None
        with loading(self.analyse_btn, "Analysing…"):
            gaps = core_api.ask_claude_json(
                prompt=user_msg, system=system, max_tokens=1024, model=model,
            )
        self._update_button_state()

        if not isinstance(gaps, list):
            self.status.setText("")
            return
        gaps = [g.strip() for g in gaps if isinstance(g, str) and g.strip()]

        # Persist last-used tag so it pre-fills next session.
        self.cfg["last_used_tag"] = tag
        save_tool_config("gap_analyser", self.cfg)

        if not gaps:
            self.gaps_box.setVisible(False)
            self.status.setText(
                f"No gaps found — your cards under '{tag}' appear to cover the LO well."
            )
            return

        self._render_gaps(gaps)
        self.status.setText(
            f"Claude found {len(gaps)} gap(s) across {len(card_fronts)} card(s) under '{tag}'."
        )

    # ── render / select / queue ──────────────────────────────────────────────

    def _render_gaps(self, gaps: list[str]):
        self.gaps_list.clear()
        for g in gaps:
            item = QListWidgetItem(g)
            item.setFlags(item.flags() | Qt.ItemFlag.ItemIsUserCheckable)
            item.setCheckState(Qt.CheckState.Checked)
            self.gaps_list.addItem(item)
        self.gaps_box.setVisible(True)

    def _select_all(self):
        for i in range(self.gaps_list.count()):
            self.gaps_list.item(i).setCheckState(Qt.CheckState.Checked)

    def _select_none(self):
        for i in range(self.gaps_list.count()):
            self.gaps_list.item(i).setCheckState(Qt.CheckState.Unchecked)

    def _dismiss(self):
        self.gaps_list.clear()
        self.gaps_box.setVisible(False)
        self.status.setText("")

    def _on_queue(self):
        approved: list[str] = []
        for i in range(self.gaps_list.count()):
            item = self.gaps_list.item(i)
            if item.checkState() == Qt.CheckState.Checked:
                t = item.text().strip()
                if t:
                    approved.append(t)
        if not approved:
            tooltip("No gaps approved.")
            return

        lo = self.lo_input.toPlainText().strip()
        tag = self.tag_input.text().strip()
        kg_items = [
            {
                "title":  gap_text,
                "source": "analyse",
                "type":   "lo",
                "status": "open",
                "lo":     lo,
                "lo_tag": tag,
                "tags":   [tag] if tag else [],
            }
            for gap_text in approved
        ]
        kg_store.add_many(kg_items)

        # If the KG panel is currently open, tell it to refresh.
        try:
            from . import knowledge_gaps
            knowledge_gaps._refresh_open_panel()
        except Exception:
            pass

        self._dismiss()
        total = kg_store.count(lambda kg: kg.get("status") == "open")
        self.status.setText(
            f"Queued {len(approved)} gap(s) to Knowledge Gaps — {total} open. "
            "Open the Knowledge Gaps tab to route each one."
        )
        tooltip(f"Queued {len(approved)} gap(s) to Knowledge Gaps.")


# ── Tool contract ────────────────────────────────────────────────────────────

_panel: GapAnalyserPanel | None = None


def init(main_window) -> None:
    return None


_scroll = None


def get_panel():
    global _panel, _scroll
    if _panel is None:
        from aqt.qt import QScrollArea
        _panel = GapAnalyserPanel()
        _scroll = QScrollArea()
        _scroll.setWidgetResizable(True)
        _scroll.setFrameShape(QScrollArea.Shape.NoFrame)
        _scroll.setWidget(_panel)
    else:
        _panel.cfg = tool_config("gap_analyser")
    _panel.refresh_setup_banner()
    return _scroll
