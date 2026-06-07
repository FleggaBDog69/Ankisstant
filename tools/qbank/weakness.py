# Weakness dashboard — "what you miss most".
#
# Aggregates captured missed questions (the MQ-typed KGs in kg_queue.json) by
# System -> Subsystem -> Topic so the user can see where they're weak. Rendered
# as a section inside the AI QBank panel. Missed questions only (type == "mq"),
# all statuses (a miss is a miss even after you've made a card).

from __future__ import annotations

from datetime import date, datetime, timedelta

from aqt.qt import (
    QComboBox, QFrame, QHBoxLayout, QLabel, QSizePolicy, Qt, QVBoxLayout,
    QWidget,
)

from ...core.config import save_tool_config, tool_config
from ...core.qt_utils import make_help_button
from ..kg import store as kg_store


UNTAGGED = "(untagged)"

# Window options offered in the selector: (label, days). 0 = all time.
_WINDOWS: list[tuple[str, int]] = [
    ("Last 7 days", 7),
    ("Last 30 days", 30),
    ("Last 90 days", 90),
    ("All time", 0),
]


# ── aggregation (pure) ─────────────────────────────────────────────────────────

def _within_window(created_at: str, since_days: int) -> bool:
    """True if `created_at` (ISO) falls within the last `since_days` days.
    `since_days <= 0` means no limit. Unparseable timestamps are kept (we'd
    rather over-count than silently drop a miss)."""
    if since_days <= 0:
        return True
    if not created_at:
        return True
    try:
        d = datetime.fromisoformat(created_at).date()
    except (ValueError, TypeError):
        return True
    return d >= date.today() - timedelta(days=since_days)


def _mq_items(items: list[dict], since_days: int) -> list[dict]:
    return [
        it for it in items
        if it.get("type") == "mq"
        and _within_window(str(it.get("created_at") or ""), since_days)
    ]


def _level(it: dict, key: str) -> str:
    val = str(kg_store.field(it, key) or "").strip()
    return val or UNTAGGED


def aggregate_misses(items: list[dict], since_days: int = 0) -> dict:
    """Nested counts of MQ captures by System -> Subsystem -> Topic.

    Returns:
        {
          "_total": int,
          systems: {
            name: {"count": int,
                   "subsystems": {name: {"count": int, "topics": {name: int}}}}
          }
        }
    """
    out: dict = {"_total": 0}
    for it in _mq_items(items, since_days):
        out["_total"] += 1
        system = _level(it, "system")
        sub    = _level(it, "subsystem")
        topic  = _level(it, "topic")

        sys_entry = out.setdefault(system, {"count": 0, "subsystems": {}})
        sys_entry["count"] += 1
        sub_entry = sys_entry["subsystems"].setdefault(sub, {"count": 0, "topics": {}})
        sub_entry["count"] += 1
        sub_entry["topics"][topic] = sub_entry["topics"].get(topic, 0) + 1
    return out


def top_topics(items: list[dict], since_days: int, n: int) -> list[tuple]:
    """Flat ranked list of (system, subsystem, topic, count) — worst first."""
    counts: dict[tuple, int] = {}
    for it in _mq_items(items, since_days):
        key = (_level(it, "system"), _level(it, "subsystem"), _level(it, "topic"))
        counts[key] = counts.get(key, 0) + 1
    ranked = sorted(counts.items(), key=lambda kv: kv[1], reverse=True)
    return [(s, sub, t, c) for (s, sub, t), c in ranked[:n]]


# ── UI ─────────────────────────────────────────────────────────────────────────

# Bar colour — a warm amber that reads on both light and dark themes.
_BAR_CSS = "background:#f59e0b;border-radius:3px;"
# Max bar width in px (mapped to the largest count in view).
_BAR_MAX = 200


class WeaknessSection(QWidget):
    """Self-contained 'Where you're weak' section for the QBank panel.

    Rebuilds itself from scratch on window change / expand toggle, mirroring
    QBankPanel.refresh()."""

    def __init__(self, parent=None):
        super().__init__(parent)
        cfg = tool_config("qbank")
        self._since = int(cfg.get("weakness_window_days", 30))
        self._top_n = max(1, int(cfg.get("weakness_top_n", 8)))
        self._expanded: set[str] = set()
        self._build()

    # ── rebuild plumbing ──────────────────────────────────────────────────────

    def _clear(self) -> None:
        old = self.layout()
        if old is not None:
            while old.count():
                item = old.takeAt(0)
                w = item.widget()
                if w is not None:
                    w.deleteLater()
            QWidget().setLayout(old)

    def _rebuild(self) -> None:
        self._clear()
        self._build()

    def _on_window_changed(self, idx: int) -> None:
        self._since = _WINDOWS[idx][1]
        try:
            cfg = tool_config("qbank")
            cfg["weakness_window_days"] = self._since
            save_tool_config("qbank", cfg)
        except Exception as e:
            print(f"[ankisstant] weakness window persist failed: {e}")
        self._rebuild()

    def _toggle(self, system: str) -> None:
        if system in self._expanded:
            self._expanded.discard(system)
        else:
            self._expanded.add(system)
        self._rebuild()

    # ── build ─────────────────────────────────────────────────────────────────

    def _build(self) -> None:
        try:
            items = kg_store.load_all()
        except Exception as e:
            print(f"[ankisstant] weakness load failed: {e}")
            items = []
        agg = aggregate_misses(items, self._since)

        root = QVBoxLayout(self)
        root.setContentsMargins(0, 6, 0, 0)
        root.setSpacing(8)

        # Header: title + window selector + help.
        header = QHBoxLayout()
        title = QLabel("<b>Where you're weak</b>")
        title.setTextFormat(Qt.TextFormat.RichText)
        header.addWidget(title)
        header.addStretch(1)

        combo = QComboBox()
        for label, _days in _WINDOWS:
            combo.addItem(label)
        cur = next((i for i, (_l, d) in enumerate(_WINDOWS) if d == self._since), 1)
        combo.setCurrentIndex(cur)
        combo.currentIndexChanged.connect(self._on_window_changed)
        header.addWidget(combo)

        header.addWidget(make_help_button(
            "Weakness dashboard — help",
            "<h3>What it shows</h3>"
            "<p>Every missed question you capture is filed under "
            "<b>System / Subsystem / Topic</b>. This aggregates them so you can "
            "see where you miss most.</p>"
            "<p>Click a system to expand its subsystem and topic breakdown. Use "
            "the time-window selector to compare recent weak spots against your "
            "all-time pattern. Counts include every captured miss, whether or not "
            "you've since made a card for it.</p>"
            "<p>Choose the default window and how many rows to show in "
            "<b>Settings → AI QBank</b>.</p>",
            self,
        ))
        root.addLayout(header)

        total = agg.get("_total", 0)
        if total == 0:
            empty = QLabel(
                "No missed questions captured in this window — hit "
                "<i>📌 Capture missed Q</i> while doing a QBank."
            )
            empty.setTextFormat(Qt.TextFormat.RichText)
            empty.setStyleSheet("color: gray;")
            empty.setWordWrap(True)
            root.addWidget(empty)
            return

        # Ranked system bars.
        systems = sorted(
            ((name, e) for name, e in agg.items() if name != "_total"),
            key=lambda kv: kv[1]["count"], reverse=True,
        )
        max_count = systems[0][1]["count"] if systems else 1
        for name, entry in systems[:self._top_n]:
            root.addWidget(self._system_row(name, entry, max_count))
            if name in self._expanded:
                root.addWidget(self._breakdown(entry))

        # Worst single topics.
        topics = top_topics(items, self._since, self._top_n)
        if topics:
            lbl = QLabel("<b>Worst topics</b>")
            lbl.setTextFormat(Qt.TextFormat.RichText)
            lbl.setStyleSheet("margin-top:6px;")
            root.addWidget(lbl)
            for system, sub, topic, count in topics:
                parts = [p for p in (system, sub, topic) if p and p != UNTAGGED]
                path = " · ".join(parts) if parts else UNTAGGED
                row = QLabel(f"{path} — <b>{count}</b>")
                row.setTextFormat(Qt.TextFormat.RichText)
                row.setStyleSheet("color: palette(text); font-size: 12px;")
                root.addWidget(row)

    def _system_row(self, name: str, entry: dict, max_count: int) -> QWidget:
        count = entry["count"]
        w = QWidget()
        w.setCursor(Qt.CursorShape.PointingHandCursor)
        row = QHBoxLayout(w)
        row.setContentsMargins(0, 0, 0, 0)
        row.setSpacing(8)

        arrow = "▾" if name in self._expanded else "▸"
        label = QLabel(f"{arrow} {name}")
        label.setMinimumWidth(140)
        label.setStyleSheet("color: palette(text); font-size: 12px;")
        row.addWidget(label)

        bar = QFrame()
        width = max(6, int(_BAR_MAX * count / max_count)) if max_count else 6
        bar.setFixedSize(width, 12)
        bar.setStyleSheet(_BAR_CSS)
        row.addWidget(bar)

        cnt = QLabel(f"<b>{count}</b>")
        cnt.setTextFormat(Qt.TextFormat.RichText)
        cnt.setStyleSheet("color: palette(text); font-size: 12px;")
        row.addWidget(cnt)
        row.addStretch(1)

        # Whole row toggles expansion.
        w.mousePressEvent = lambda _ev, n=name: self._toggle(n)
        return w

    def _breakdown(self, entry: dict) -> QWidget:
        w = QWidget()
        col = QVBoxLayout(w)
        col.setContentsMargins(24, 0, 0, 4)
        col.setSpacing(2)
        subs = sorted(entry["subsystems"].items(),
                      key=lambda kv: kv[1]["count"], reverse=True)
        for sub_name, sub_entry in subs:
            sub_lbl = QLabel(f"{sub_name} — <b>{sub_entry['count']}</b>")
            sub_lbl.setTextFormat(Qt.TextFormat.RichText)
            sub_lbl.setStyleSheet("color: palette(text); font-size: 12px;")
            col.addWidget(sub_lbl)
            topics = sorted(sub_entry["topics"].items(),
                            key=lambda kv: kv[1], reverse=True)
            for topic_name, tcount in topics:
                t_lbl = QLabel(f"{topic_name} — {tcount}")
                t_lbl.setStyleSheet(
                    "color: gray; font-size: 11px; margin-left: 16px;")
                col.addWidget(t_lbl)
        w.setSizePolicy(QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Fixed)
        return w
