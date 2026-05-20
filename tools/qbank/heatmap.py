# Heatmap injected below the Anki deck list.
# Window is dynamic: ±5 months around today, scrollable via prev/today/next.
# Past days: green scale (relative quantity) + white dot when target hit.
# Future days: gray scale showing planned intensity (heavier target = darker).
# Exam dates: red.

from __future__ import annotations

import calendar
from datetime import date, timedelta

from ..kg import store as kg_store
from .stats import load_combined_stats, get_streak
from .targets import get_target, get_exam_dates


def _practice_questions_available() -> bool:
    """True iff the practice_questions addon is installed and importable.
    Used to surface its launcher button + roll its stats into the heatmap."""
    try:
        import practice_questions  # noqa: F401
        return True
    except Exception:
        return False


# Synthetic platform entry merged into the stats list when practice_questions
# is present. Key matches the file practice_questions writes to
# (`stats_practice_questions.json` inside ankisstant/user_files/).
_PRACTICE_QS_PLATFORM = {"key": "practice_questions", "name": "Practice Qs"}


def _qbank_open_kg_count() -> int:
    return kg_store.count(
        lambda kg: kg.get("source") == "qbank" and kg.get("status") == "open"
    )

CELL = 10
GAP  = 2
STEP = CELL + GAP  # 12 px per column

DOW_LABELS = ["M", "T", "W", "T", "F", "S", "S"]

# Module-level scroll state (negative = past, positive = future). Reset to 0
# on profile load by the main __init__.
_offset_months: int = 0


def shift_offset(delta: int) -> None:
    global _offset_months
    _offset_months += delta


def reset_offset() -> None:
    global _offset_months
    _offset_months = 0


def _add_months(d: date, months: int) -> date:
    total = d.month - 1 + months
    year  = d.year + total // 12
    month = total % 12 + 1
    day   = min(d.day, calendar.monthrange(year, month)[1])
    return date(year, month, day)


def _past_color(count: int, max_count: int) -> str:
    if count <= 0:
        return "ec-c0"
    if max_count <= 0:
        return "ec-c1"
    r = count / max_count
    if r < 0.25:
        return "ec-c1"
    if r < 0.5:
        return "ec-c2"
    if r < 0.75:
        return "ec-c3"
    return "ec-c4"


def _future_color(day_target: int, max_target: int) -> str:
    if day_target <= 0 or max_target <= 0:
        return "ec-ft0"
    r = day_target / max_target
    if r < 0.4:
        return "ec-ft1"
    if r < 0.7:
        return "ec-ft2"
    return "ec-ft3"


def _compute_window(stats: dict, cfg: dict) -> tuple[date, int]:
    today = date.today()
    off   = _offset_months

    raw_start = _add_months(today, -5 + off)
    raw_end   = _add_months(today,  5 + off)

    exam_objs = get_exam_dates(cfg)
    for e in exam_objs:
        ed = e["date"]
        if raw_start <= ed <= _add_months(today, 7 + off):
            raw_end = max(raw_end, ed + timedelta(weeks=1))

    start = raw_start - timedelta(days=raw_start.weekday())          # Monday-align
    end   = raw_end   + timedelta(days=(6 - raw_end.weekday()) % 7)  # Sunday-align
    num_weeks = max(8, (end - start).days // 7 + 1)
    return start, num_weeks


def build_card_html(platforms: list, cfg: dict) -> str:
    """Return the full heatmap card HTML (CSS inline). The Ankisstant button is
    rendered separately by the caller — this function only returns the heatmap."""
    pq_present = _practice_questions_available()
    stats_platforms = list(platforms) + ([_PRACTICE_QS_PLATFORM] if pq_present else [])
    stats  = load_combined_stats(stats_platforms)
    today  = date.today()
    start, num_weeks = _compute_window(stats, cfg)

    # Summary — past window only
    start_iso = start.isoformat()
    today_iso = today.isoformat()
    past_stats = {k: v for k, v in stats.items() if start_iso <= k <= today_iso}
    max_count   = max((v.get("total", 0) for v in past_stats.values()), default=0)
    total_q     = sum(v.get("total", 0) for v in past_stats.values())
    total_cor   = sum(v.get("correct", 0) for v in past_stats.values())
    accuracy    = (total_cor / total_q * 100) if total_q else 0
    active_days = sum(1 for v in past_stats.values() if v.get("total", 0) > 0)
    cur_streak, best_streak = get_streak(stats)
    streak_word = "day" if cur_streak == 1 else "days"

    today_target = get_target(today, cfg)
    today_done   = stats.get(today_iso, {}).get("total", 0)
    if today_target > 0:
        remaining = max(0, today_target - today_done)
        if today_done >= today_target:
            today_line = f'<div class="ec-today-line ec-on-target">Today: {today_done} / {today_target} ✓</div>'
        else:
            today_line = f'<div class="ec-today-line">Today: {today_done} / {today_target} &nbsp;({remaining} to go)</div>'
    else:
        today_line = f'<div class="ec-today-line">Today: {today_done} Qs</div>'

    max_target = max(
        cfg.get("default_daily", 0),
        max((p.get("daily", 0) for p in cfg.get("target_periods", [])), default=0),
    )

    exam_objs = get_exam_dates(cfg)
    exam_set  = {e["date"]: e["label"] for e in exam_objs}

    # ── Grid ──────────────────────────────────────────────────────────────────
    weeks_html    = ""
    month_labels: dict[int, str] = {}

    for col in range(num_weeks):
        week_monday = start + timedelta(weeks=col)
        days_html = ""

        if col == 0:
            is_new_month = True
        else:
            is_new_month = (week_monday - timedelta(weeks=1)).month != week_monday.month
        if is_new_month:
            month_labels[col] = week_monday.strftime("%b '%y")

        for row in range(7):
            d   = week_monday + timedelta(days=row)
            iso = d.isoformat()

            entry     = stats.get(iso, {})
            count     = entry.get("total", 0)
            correct   = entry.get("correct", 0)
            incorrect = entry.get("incorrect", 0)
            is_future = d > today
            is_exam   = d in exam_set

            if is_exam:
                cls   = "ec-exam"
                label = exam_set[d]
                if is_future:
                    tip = f"{iso}: {label} 🔴"
                else:
                    base = f"{count} Qs ({correct}✓ {incorrect}✗)" if count else "no activity"
                    tip  = f"{iso}: {label} 🔴 — {base}"
                tick = ""

            elif is_future:
                day_target = get_target(d, cfg)
                cls  = _future_color(day_target, max_target)
                tip  = f"{iso}: planned {day_target} Qs" if day_target > 0 else f"{iso}: (no target)"
                tick = ""

            else:
                day_target = get_target(d, cfg)
                cls  = _past_color(count, max_count)
                hit  = day_target > 0 and count >= day_target
                tick = '<div class="ec-tick"></div>' if hit else ""
                if count > 0:
                    progress = (
                        " — target ✓" if hit else
                        f" — {count/day_target*100:.0f}% of {day_target}" if day_target > 0 else ""
                    )
                    tip = f"{iso}: {count} Qs ({correct}✓ {incorrect}✗){progress}"
                else:
                    tip = f"{iso}: no activity"
                    if day_target > 0:
                        tip += f" (target: {day_target})"

            today_cls = " ec-today" if d == today else ""
            days_html += f'<div class="ec-day {cls}{today_cls}" title="{tip}">{tick}</div>'

        week_cls = "ec-week"
        if col > 0 and col in month_labels:
            week_cls += " ec-month-start"
        weeks_html += f'<div class="{week_cls}">{days_html}</div>'

    months_html = ""
    bar_width   = num_weeks * STEP
    seen: set[str] = set()
    for col, label in sorted(month_labels.items()):
        if label in seen:
            continue
        seen.add(label)
        months_html += f'<span class="ec-mlabel" style="left:{col * STEP}px">{label}</span>'

    dow_html = "".join(f'<span class="ec-dow-cell">{l}</span>' for l in DOW_LABELS)

    # ── Buttons ───────────────────────────────────────────────────────────────
    launch_buttons = " ".join(
        f'<button class="ec-btn" onclick="pycmd(\'qbank:open:{p["key"]}\')"'
        f' title="{p["name"]}{ " (Q)" if i == 0 else "" }">{p["name"]}{ " <kbd>Q</kbd>" if i == 0 else "" }</button>'
        for i, p in enumerate(platforms)
    )
    practice_qs_btn = (
        '<button class="ec-btn" onclick="pycmd(\'practice_questions:open\')"'
        ' title="Practice Questions addon — local screenshot library">Practice Qs</button>'
        if pq_present else ""
    )
    settings_btn = '<button class="ec-btn ec-settings-btn" onclick="pycmd(\'ankisstant:settings\')" title="Settings">⚙</button>'
    log_btn      = '<button class="ec-btn" onclick="pycmd(\'qbank:log_session\')" title="Log a session manually (e.g. from phone)">+ Log</button>'

    n_queue = _qbank_open_kg_count()
    review_btn = (
        f'<button class="ec-btn ec-review-btn" onclick="pycmd(\'ankisstant:open_kg:qbank\')" title="Review captured missed questions">📌 {n_queue}</button>'
        if n_queue > 0 else ""
    )
    # Gate the home-screen ＋ KG button on the Knowledge Gaps setting.
    from ...core.config import tool_config as _tc
    _kg_cfg = _tc("knowledge_gaps")
    add_kg_btn = (
        '<button class="ec-btn" onclick="pycmd(\'ankisstant:add_kg\')" title="Add a knowledge gap">＋ KG</button>'
        if _kg_cfg.get("enabled", True) and _kg_cfg.get("show_home_button", True)
        else ""
    )

    today_btn = (
        f'<button class="ec-btn ec-nav-today" onclick="pycmd(\'qbank:heatmap:today\')" title="Jump to today">Today</button>'
        if _offset_months != 0 else ""
    )
    nav_buttons = (
        f'<button class="ec-btn ec-nav-btn" onclick="pycmd(\'qbank:heatmap:prev\')" title="Previous month">‹</button>'
        f'{today_btn}'
        f'<button class="ec-btn ec-nav-btn" onclick="pycmd(\'qbank:heatmap:next\')" title="Next month">›</button>'
    )

    return f"""
<div style="display:flex;justify-content:center;width:100%;">
<style>
  .ec-card {{
    background: var(--canvas, #fff);
    border: 1px solid rgba(127,127,127,0.2);
    border-radius: 10px;
    padding: 12px 16px 10px;
    margin: 10px auto 18px;
    width: fit-content;
    font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
  }}
  .ec-header {{
    display: flex;
    justify-content: space-between;
    align-items: center;
    margin-bottom: 10px;
    gap: 16px;
    flex-wrap: wrap;
  }}
  .ec-title  {{ font-size: 14px; font-weight: 600; opacity: 0.9; }}
  .ec-meta   {{ display: flex; flex-direction: column; gap: 2px; }}
  .ec-summary, .ec-streak {{ font-size: 11px; opacity: 0.6; }}
  .ec-summary b, .ec-streak b {{ opacity: 0.9; font-weight: 600; }}
  .ec-today-line  {{ font-size: 11px; opacity: 0.65; margin-top: 1px; }}
  .ec-on-target   {{ color: #26a641; opacity: 0.9 !important; font-weight: 600; }}
  .night_mode .ec-on-target {{ color: #39d353; }}
  .ec-body {{ display: flex; gap: 4px; align-items: flex-start; justify-content: center; }}
  .ec-nav-row {{ display: flex; justify-content: center; gap: 4px; margin-top: 8px; }}
  .ec-dow  {{ display: flex; flex-direction: column; gap: {GAP}px; padding-top: 1px; }}
  .ec-dow-cell {{
    display: block; height: {CELL}px; line-height: {CELL}px;
    font-size: 9px; opacity: 0.45; text-align: right; width: 8px;
  }}
  .ec-grid-wrap {{ display: flex; flex-direction: column; }}
  .ec-grid  {{ display: flex; flex-direction: row; gap: {GAP}px; }}
  .ec-week  {{ display: flex; flex-direction: column; gap: {GAP}px; position: relative; }}
  .ec-week.ec-month-start::before {{
    content: ''; position: absolute; left: -1px; top: 0; bottom: 0;
    width: 1px; background: rgba(127,127,127,0.09); pointer-events: none;
  }}
  .night_mode .ec-week.ec-month-start::before {{ background: rgba(255,255,255,0.07); }}
  .ec-day   {{ width: {CELL}px; height: {CELL}px; border-radius: 2px; position: relative; }}
  .ec-today {{ outline: 1px solid rgba(0,0,0,0.45); outline-offset: -1px; }}
  .night_mode .ec-today {{ outline-color: rgba(255,255,255,0.45); }}
  .ec-tick {{
    position: absolute; top: 2px; right: 2px;
    width: 3px; height: 3px; border-radius: 50%;
    background: rgba(255,255,255,0.85); pointer-events: none;
  }}
  .ec-c0 {{ background-color: #ebedf0; }}
  .ec-c1 {{ background-color: #9be9a8; }}
  .ec-c2 {{ background-color: #40c463; }}
  .ec-c3 {{ background-color: #30a14e; }}
  .ec-c4 {{ background-color: #216e39; }}
  .night_mode .ec-c0 {{ background-color: #2d333b; }}
  .night_mode .ec-c1 {{ background-color: #0e4429; }}
  .night_mode .ec-c2 {{ background-color: #006d32; }}
  .night_mode .ec-c3 {{ background-color: #26a641; }}
  .night_mode .ec-c4 {{ background-color: #39d353; }}
  .ec-ft0 {{ background-color: #ebedf0; opacity: 0.3; }}
  .ec-ft1 {{ background-color: #c6cad2; }}
  .ec-ft2 {{ background-color: #9499a6; }}
  .ec-ft3 {{ background-color: #5c6370; }}
  .night_mode .ec-ft0 {{ background-color: #2d333b; opacity: 0.3; }}
  .night_mode .ec-ft1 {{ background-color: #3d4451; }}
  .night_mode .ec-ft2 {{ background-color: #4f5768; }}
  .night_mode .ec-ft3 {{ background-color: #656d7c; }}
  .ec-exam {{ background-color: #cf222e !important; opacity: 0.85; }}
  .night_mode .ec-exam {{ background-color: #f85149 !important; }}
  .ec-months  {{ position: relative; height: 16px; width: {bar_width}px; margin-top: 4px; }}
  .ec-mlabel  {{ position: absolute; font-size: 10px; opacity: 0.5; white-space: nowrap; }}
  .ec-btns {{ display: flex; gap: 6px; align-items: center; }}
  .ec-btn {{
    font-size: 12px; padding: 4px 12px; border-radius: 6px;
    border: 1px solid rgba(127,127,127,0.3);
    background: var(--button-bg, #f6f8fa);
    color: inherit; cursor: pointer; font-family: inherit; font-weight: 500;
  }}
  .ec-btn:hover {{ background: var(--button-hover-bg, #eaeef2); }}
  .night_mode .ec-btn {{ background: #2d333b; border-color: rgba(255,255,255,0.15); }}
  .night_mode .ec-btn:hover {{ background: #383f47; }}
  .ec-settings-btn {{ padding: 4px 8px !important; opacity: 0.6; }}
  .ec-settings-btn:hover {{ opacity: 1 !important; }}
  .ec-nav-btn {{ padding: 4px 9px !important; font-size: 14px; line-height: 1; }}
  .ec-nav-today {{ font-size: 11px; opacity: 0.7; }}
  .ec-review-btn {{ background: #fef3c7 !important; color: #92400e !important; font-weight: 600; }}
  .night_mode .ec-review-btn {{ background: #78350f !important; color: #fef3c7 !important; }}
  .ec-btn kbd {{ font-size: 9px; opacity: 0.55; border: 1px solid currentColor; padding: 0 3px; border-radius: 3px; margin-left: 4px; font-family: inherit; }}
</style>
<div class="ec-card">
  <div class="ec-header">
    <div class="ec-meta">
      <div class="ec-summary">
        <b>{accuracy:.0f}%</b> correct &middot; <b>{active_days}</b> active days
      </div>
      <div class="ec-streak">
        Streak: <b>{cur_streak}</b> {streak_word} &ensp;&middot;&ensp; Best: <b>{best_streak}</b> days
      </div>
      {today_line}
    </div>
    <div class="ec-btns">{review_btn}{add_kg_btn}{launch_buttons}{practice_qs_btn}{log_btn}{settings_btn}</div>
  </div>
  <div class="ec-body">
    <div class="ec-dow">{dow_html}</div>
    <div class="ec-grid-wrap">
      <div class="ec-grid">{weeks_html}</div>
      <div class="ec-months">{months_html}</div>
    </div>
  </div>
  <div class="ec-nav-row">{nav_buttons}</div>
</div>
</div>
"""
