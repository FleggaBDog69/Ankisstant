# Single source of truth for reading/writing Ankisstant config.
# Tools and UI go through here — no direct mw.addonManager.getConfig calls elsewhere.

from __future__ import annotations

from aqt import mw

ADDON = "ankisstant"

DEFAULTS: dict = {
    "schema_version": 1,
    "migrated_v1": False,
    "first_run_seen": False,
    "debug_logging": False,
    "anthropic_api_key": "",
    "claude_cli_path": "",
    "claude_cli_extra_args": [],
    "provider_mode": "auto",            # "auto" | "cli" | "api"
    "model_default": "claude-sonnet-4-6",
    "window": {"width": 900, "height": 600, "x": None, "y": None},
    "tools": {
        "qbank": {
            "enabled": True,
            "show_heatmap": True,
            "platforms": [
                {"key": "osmosis",     "name": "Osmosis",     "url": "https://www.osmosis.org/"},
                {"key": "emedici",     "name": "eMedici",     "url": "https://app.emedici.com"},
                {"key": "clinicalkey", "name": "ClinicalKey", "url": "https://www.clinicalkey.com/student/questionbanks"},
            ],
            "default_daily": 0,
            "target_periods": [],
            "exam_dates": [],
            "search_model": "claude-haiku-4-5-20251001",
            "card_gen_model": "claude-sonnet-4-6",
            "card_skill_id": "",
            "card_prompt": "",
            "card_notetype": "",
            "card_deck": "",
            "missed_q_field": "Missed Questions",
            "tag_root": "Missed_Questions",
            "last_system": "",
            "last_subsystem": "",
            "last_topic": "",
            "ai_last_selected": "Claude",
            "ai_panel_visible": False,
        },
        "browse": {
            "enabled": True,
            "model": "claude-sonnet-4-6",
            "last_used_tag": "",
            "max_results": 50,
            "notetype_filter": "",
            "front_field": "Text",
            "audit_tag": "Ankisstant::AI::Browse",
            "enable_gap_report": False,
            "source_tags": [],
        },
        "gap_analyser": {
            "enabled": True,
            "model": "claude-sonnet-4-6",
            "front_field": "Text",
            "notetype_filter": "",
            "last_used_tag": "",
            "max_cards": 80,
            "max_gaps": 10,
        },
        "knowledge_gaps": {
            "enabled": True,
            "default_status_on_add": "open",
            "confirm_on_delete": True,
            "show_home_button": True,
        },
        "card_creator": {
            "enabled": True,
            "model": "claude-sonnet-4-6",
            "default_deck": "",
            "default_notetype": "",
            "default_tags": [],
            "audit_tag": "Ankisstant::AI::Creator",
            "default_n_cards": 10,
            "gap_n_cards": 3,
            "front_field": "Text",
            "extra_field": "Extra",
            "one_by_one_field": "One by one",
            # Curated list of notetype profiles surfaced in the creator's
            # dropdown. Each profile maps a notetype name to its field layout
            # plus an optional prompt addendum so Claude can tailor output
            # (e.g. a different style for the Malleus deck). The legacy
            # `default_notetype` + top-level field names above seed the first
            # profile on migration; new installs start with an empty list.
            "notetypes": [],
            "selected_notetype": "",
        },
    },
}


def _deep_merge(base: dict, override: dict) -> dict:
    """Recursively merge override into a copy of base. Lists are replaced, not merged."""
    out = dict(base)
    for k, v in (override or {}).items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = _deep_merge(out[k], v)
        else:
            out[k] = v
    return out


def ensure_config() -> None:
    """Write any missing default keys back to disk so the user can edit them in
    Anki's addon config dialog."""
    raw = mw.addonManager.getConfig(ADDON) or {}
    merged = _deep_merge(DEFAULTS, raw)
    if merged != raw:
        mw.addonManager.writeConfig(ADDON, merged)


def load_config() -> dict:
    raw = mw.addonManager.getConfig(ADDON) or {}
    cfg = _deep_merge(DEFAULTS, raw)
    _migrate_creator_notetypes(cfg)
    return cfg


def _migrate_creator_notetypes(cfg: dict) -> None:
    """If the user has a legacy single `default_notetype` but no `notetypes`
    profile list yet, seed the list with one profile derived from the
    top-level field names. Idempotent — runs every load_config but only
    mutates on the first call after upgrade."""
    cc = cfg.get("tools", {}).get("card_creator")
    if not isinstance(cc, dict):
        return
    if cc.get("notetypes"):
        return
    legacy_name = (cc.get("default_notetype") or "").strip()
    if not legacy_name:
        return
    cc["notetypes"] = [{
        "name":             legacy_name,
        "front_field":      cc.get("front_field", "Text"),
        "extra_field":      cc.get("extra_field", "Extra"),
        "one_by_one_field": cc.get("one_by_one_field", "One by one"),
        "image_field":      cc.get("extra_field", "Extra"),
        "extra_instructions": "",
    }]
    if not cc.get("selected_notetype"):
        cc["selected_notetype"] = legacy_name


def save_config(cfg: dict) -> None:
    mw.addonManager.writeConfig(ADDON, cfg)


def tool_config(tool_name: str) -> dict:
    """Return the merged config slice for `tool_name`, falling back to defaults."""
    cfg = load_config()
    return cfg.get("tools", {}).get(tool_name, {})


def save_tool_config(tool_name: str, tool_cfg: dict) -> None:
    cfg = load_config()
    cfg.setdefault("tools", {})[tool_name] = tool_cfg
    save_config(cfg)


def tool_enabled(tool_name: str) -> bool:
    return bool(tool_config(tool_name).get("enabled", False))


def set_window_geometry(width: int, height: int, x: int | None, y: int | None) -> None:
    cfg = load_config()
    cfg["window"] = {"width": int(width), "height": int(height), "x": x, "y": y}
    save_config(cfg)


def get_window_geometry() -> dict:
    return load_config().get("window", DEFAULTS["window"])
