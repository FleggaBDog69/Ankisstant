# Per-day question counts, one JSON file per platform.
# Format: { "YYYY-MM-DD": { "correct": int, "incorrect": int, "total": int, "sessions": int } }
# Files live in ankisstant/user_files/.
# Backward compat: "emedici" uses stats.json (the original file name) so migrated
# data is found in place.

from __future__ import annotations

import json
import os
from datetime import date, timedelta


def _user_files() -> str:
    """ankisstant/user_files — three levels up from this file (tools/qbank/stats.py)."""
    here = os.path.dirname(os.path.abspath(__file__))
    root = os.path.dirname(os.path.dirname(here))
    uf = os.path.join(root, "user_files")
    os.makedirs(uf, exist_ok=True)
    return uf


def _stats_path(platform: str = "emedici") -> str:
    filename = "stats.json" if platform == "emedici" else f"stats_{platform}.json"
    return os.path.join(_user_files(), filename)


def ensure_storage(platform: str = "emedici") -> None:
    path = _stats_path(platform)
    if not os.path.exists(path):
        with open(path, "w") as f:
            json.dump({}, f)


def load_stats(platform: str = "emedici") -> dict:
    try:
        with open(_stats_path(platform), "r") as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def save_stats(data: dict, platform: str = "emedici") -> None:
    with open(_stats_path(platform), "w") as f:
        json.dump(data, f, indent=2)


def add_session(correct: int, incorrect: int, platform: str = "emedici",
                on_date: str | None = None) -> None:
    """Record a session, merged into the given date's totals (defaults to today)."""
    if correct <= 0 and incorrect <= 0:
        return
    data = load_stats(platform)
    iso = on_date or date.today().isoformat()
    entry = data.get(iso, {"correct": 0, "incorrect": 0, "total": 0, "sessions": 0})
    entry["correct"]   += correct
    entry["incorrect"] += incorrect
    entry["total"]      = entry["correct"] + entry["incorrect"]
    entry["sessions"]   = entry.get("sessions", 0) + 1
    data[iso] = entry
    save_stats(data, platform)


def discover_stats_keys() -> list[str]:
    """Every platform key that has a stats file on disk — including ad-hoc
    "Other / Manual" entries that aren't in the platforms config."""
    keys: list[str] = []
    try:
        for name in os.listdir(_user_files()):
            if name == "stats.json":
                keys.append("emedici")
            elif name.startswith("stats_") and name.endswith(".json"):
                keys.append(name[len("stats_"):-len(".json")])
    except FileNotFoundError:
        pass
    return keys


def load_combined_stats(platforms: list) -> dict:
    """Merge stats by date across all on-disk stats files.

    `platforms` is accepted for back-compat but ignored — we discover every
    stats file so ad-hoc sources (e.g. "Other / Manual: UWorld") show up on
    the heatmap without needing to be registered in the platforms config."""
    combined: dict = {}
    keys = set(discover_stats_keys())
    for p in platforms or []:
        keys.add(p["key"])
    for key in keys:
        for iso, entry in load_stats(key).items():
            day = combined.setdefault(iso, {"correct": 0, "incorrect": 0, "total": 0, "sessions": 0})
            day["correct"]   += entry.get("correct", 0)
            day["incorrect"] += entry.get("incorrect", 0)
            day["total"]     += entry.get("total", 0)
            day["sessions"]  += entry.get("sessions", 0)
    return combined


def get_streak(stats: dict) -> tuple[int, int]:
    """Return (current_streak, longest_streak) in days."""
    today = date.today()

    current_streak = 0
    check = today
    while stats.get(check.isoformat(), {}).get("total", 0) > 0:
        current_streak += 1
        check -= timedelta(days=1)

    active = sorted(k for k, v in stats.items() if v.get("total", 0) > 0)
    longest_streak = 0
    run = 0
    prev = None
    for iso in active:
        d = date.fromisoformat(iso)
        if prev is not None and (d - prev).days == 1:
            run += 1
        else:
            run = 1
        if run > longest_streak:
            longest_streak = run
        prev = d

    return current_streak, longest_streak
