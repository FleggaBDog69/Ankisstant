# Pure helpers for daily targets and exam dates. Used by the heatmap.
# The QBank-specific config tabs that edit these values live in ui/settings.py.

from __future__ import annotations

from datetime import date


def get_exam_dates(cfg: dict) -> list[dict]:
    """Return [{"date": date_obj, "label": str}, ...] for all configured exam dates."""
    result = []
    for entry in cfg.get("exam_dates", []):
        try:
            result.append({
                "date":  date.fromisoformat(entry["date"]),
                "label": entry.get("label", "Exam"),
            })
        except (KeyError, ValueError):
            continue
    return result


def get_target(d: date, cfg: dict) -> int:
    """Return the daily Q target for date d (0 = no target set).
    Exam-period overrides take precedence; the default applies outside any period."""
    for p in cfg.get("target_periods", []):
        try:
            if date.fromisoformat(p["from"]) <= d <= date.fromisoformat(p["to"]):
                return p.get("daily", 0)
        except (KeyError, ValueError):
            continue
    return cfg.get("default_daily", 0)
