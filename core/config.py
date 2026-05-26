# Single source of truth for reading/writing Ankisstant config.
# Tools and UI go through here — no direct mw.addonManager.getConfig calls elsewhere.

from __future__ import annotations

from aqt import mw

# The add-on's package/folder name. When installed from AnkiWeb this is the
# numeric ID (e.g. "351752439"); in dev it's "ankisstant". Derive it from the
# module path so getConfig/writeConfig always target the real folder — never
# hardcode it, or meta.json writes hit a folder that doesn't exist.
ADDON = __name__.split(".")[0]

# ── provider catalogue ─────────────────────────────────────────────────────────
# Known models per provider family, surfaced in the model pickers. The combos
# stay editable so any custom/newer model ID can still be typed. Defined here
# (the lowest layer) so both core/api.py and ui/settings.py can import them
# without a circular dependency.
PROVIDER_MODELS: dict[str, list[str]] = {
    "anthropic": ["claude-opus-4-7", "claude-sonnet-4-6", "claude-haiku-4-5-20251001"],
    "gemini":    ["gemini-2.5-pro", "gemini-2.5-flash", "gemini-2.0-flash"],
    "openai":    ["gpt-4o", "gpt-4o-mini", "gpt-4.1-mini"],
    # Ollama runs whatever models the user has pulled locally; these are common
    # suggestions only — the picker stays editable so any pulled model works.
    "ollama":    ["llama3.1", "llama3.2", "qwen2.5", "mistral", "gemma2"],
}

# A "provider" (the user-selected mode) maps to one model family. "auto"/"cli"
# are Claude-only modes, so both resolve to the anthropic family.
PROVIDER_OF: dict[str, str] = {
    "auto":      "anthropic",
    "cli":       "anthropic",
    "anthropic": "anthropic",
    "gemini":    "gemini",
    "openai":    "openai",
    "ollama":    "ollama",
    # "manual" = no in-app AI: the user runs the prompt in their own external
    # LLM and pastes the reply back. No model is ever sent, so it borrows the
    # anthropic family purely to keep resolve_model() happy.
    "manual":    "anthropic",
}

# Per-family defaults for "fast" (search/cheap) and "smart" (generation) roles.
# gemini defaults to 2.5-flash for BOTH roles: on the free tier neither
# gemini-2.0-flash nor gemini-2.5-pro has any quota (both return 429 limit:0),
# so neither can be a safe default. 2.5-flash is the only free-tier-capable
# Gemini model. Pro stays selectable in the picker for users on a paid plan.
_FAST_MODELS:  dict[str, str] = {"anthropic": "claude-haiku-4-5-20251001", "gemini": "gemini-2.5-flash", "openai": "gpt-4o-mini", "ollama": "llama3.1"}
_SMART_MODELS: dict[str, str] = {"anthropic": "claude-sonnet-4-6",          "gemini": "gemini-2.5-flash", "openai": "gpt-4o",      "ollama": "llama3.1"}

DEFAULTS: dict = {
    "schema_version": 1,
    "migrated_v1": False,
    # One-time heal of Gemini model IDs that have no free-tier quota (see
    # _migrate_dead_gemini_models). Flips to True after the first rewrite so a
    # paying user can re-select Pro afterwards and have it stick.
    "gemini_freetier_migrated": False,
    "first_run_seen": False,
    "debug_logging": False,
    "anthropic_api_key": "",
    "gemini_api_key": "",
    "openai_api_key": "",
    "claude_cli_path": "",
    "claude_cli_extra_args": [],
    # Base URL of the local Ollama server. No API key — Ollama is keyless.
    "ollama_url": "http://localhost:11434",
    # "auto" | "cli" | "anthropic" | "gemini" | "openai" | "ollama" | "manual"
    # (auto/cli are Claude-only; anthropic/gemini/openai/ollama are direct API
    # providers; manual = copy/paste with the user's own external AI, no key)
    "provider": "auto",
    # Default model per provider family, used when a tool passes no explicit model.
    "model_defaults": dict(_FAST_MODELS),
    # When on, every card created (Create) or unsuspended/tagged (Browse) also
    # gets a month tag "<prefix>::<YYYY-MM>" so cards carry a sense of when
    # they entered your rotation. The prefix is user-configurable.
    "month_tag_enabled": True,
    "month_tag_prefix": "Ankisstant::Month",
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
            "search_model": dict(_FAST_MODELS),
            "card_gen_model": dict(_SMART_MODELS),
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
            # Max width applied to pasted screenshots when saved into a card.
            "image_max_width": 300,
            # Zoom factor applied to the QBank browser webview while the capture
            # popup is open (so more of the question fits on screen for a
            # screenshot). The prior zoom is restored when the popup closes.
            # Anki's own reviewer is never touched.
            "capture_zoom_factor": 0.7,
            # Last user-dragged position of the capture popup. {"x": int, "y": int}
            # or None for "use default placement".
            "capture_window_pos": None,
            # Application-wide shortcut that opens the capture popup, even while
            # reviewing. Empty string disables the shortcut.
            "capture_shortcut": "Ctrl+M",
        },
        "browse": {
            "enabled": True,
            "model": dict(_SMART_MODELS),
            "last_used_tag": "",
            "max_results": 50,
            "notetype_filter": "",
            "front_field": "Text",
            "audit_tag": "Ankisstant::AI::Browse",
            "source_tags": [],
            # Side-effect toggles on the Confirm button. Default to current
            # behaviour (all on) so existing users see no change.
            "auto_unsuspend":       True,
            "auto_audit_tag":       True,
            "auto_grade_again_mq":  True,
            # Hierarchical auto-tag master switch for Browse. When on, a topic
            # search also asks the AI for {system, subsystem, topic} and
            # pre-fills the "Tag to apply" field using the SHARED scheme in
            # knowledge_gaps (auto_tag_base + tag_scheme_template). The type
            # segment comes from the loaded KG, or defaults to "KG" for a free
            # search. There is no Browse-specific prefix anymore.
            "auto_tag":        True,
        },
        "gap_analyser": {
            "enabled": True,
            "model": dict(_SMART_MODELS),
            "front_field": "Text",
            "notetype_filter": "",
            "last_used_tag": "",
            "max_cards": 80,
            "max_gaps": 10,
        },
        "knowledge_gaps": {
            "enabled": True,
            "default_status_on_add": "open",
            "default_type_on_add": "kg",
            "confirm_on_delete": True,
            "show_home_button": True,
            # Auto-tag generation — the SINGLE source of truth for the tag
            # scheme used by BOTH Create and Browse. When a KG's type has
            # `auto_tag: true`, Claude extracts {system, subsystem, topic} from
            # the KG title + stem and slots them into the template below.
            #   {base} — the shared base prefix (auto_tag_base)
            #   {type} — the KG type's display name (MQ / KG / LO)
            #   {system}/{subsystem}/{topic} — the extracted levels
            # Cards from Create or Browse therefore sit together under
            # <base>::MQ / <base>::KG, distinguished by type, not by tool.
            "auto_tag_base": "!!Fleg",
            "tag_scheme_template": "{base}::{type}::{system}::{subsystem}::{topic}",
            # Configurable list of KG types. Each item:
            #   key         — stable id (slug); used by KG entries
            #   name        — display label
            #   color       — hex string for the badge background
            #   description — long-form help text shown in the type picker
            #   fields      — list of {key, label, kind, placeholder} defining
            #                 the schema for KGs of this type. `kind` is one of
            #                 text | longtext | html | url | tag.
            # Users can add/edit/delete these in Ankisstant Settings → Knowledge Gaps.
            "types": [
                {
                    "key": "mq", "name": "MQ", "color": "#b45309",
                    "description": "Missed question — captured from a QBank or recall.",
                    "auto_tag": True,
                    "fields": [
                        {"key": "concept",   "label": "Concept missed",  "kind": "text",
                         "placeholder": "e.g. digoxin toxicity worsened by hypokalaemia"},
                        {"key": "stem_html", "label": "Question stem",   "kind": "html",
                         "placeholder": "Paste text or a screenshot (Cmd/Ctrl+V)"},
                        {"key": "system",    "label": "System",          "kind": "text",
                         "placeholder": "e.g. Cardio"},
                        {"key": "subsystem", "label": "Subsystem",       "kind": "text",
                         "placeholder": "e.g. Arrhythmia"},
                        {"key": "topic",     "label": "Topic",           "kind": "text",
                         "placeholder": "e.g. Digoxin"},
                        {"key": "platform",  "label": "QBank source",    "kind": "text",
                         "placeholder": "e.g. AMBOSS, eMedici"},
                        {"key": "notes",     "label": "Notes",           "kind": "longtext",
                         "placeholder": "Optional context, mnemonic, why you got it wrong"},
                    ],
                },
                {
                    "key": "kg", "name": "KG", "color": "#6b7280",
                    "description": "Knowledge gap — anything you don't know yet.",
                    "auto_tag": True,
                    "fields": [
                        {"key": "notes",     "label": "Notes",           "kind": "longtext",
                         "placeholder": "What specifically don't you know?"},
                    ],
                },
                {
                    "key": "lo", "name": "LO", "color": "#9333ea",
                    "description": "Learning objective — a curriculum statement not yet covered.",
                    "auto_tag": False,
                    "fields": [
                        {"key": "lo",        "label": "Learning objective", "kind": "longtext",
                         "placeholder": "Paste the LO verbatim from the curriculum"},
                        {"key": "lo_tag",    "label": "Anki tag for LO",    "kind": "tag",
                         "placeholder": "School::Year3::Cardio::BetaBlockers"},
                        {"key": "notes",     "label": "Notes",              "kind": "longtext",
                         "placeholder": "What part of the LO isn't covered yet?"},
                    ],
                },
            ],
        },
        "card_creator": {
            "enabled": True,
            "model": dict(_SMART_MODELS),
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
    _migrate_auto_tag_scheme(merged)
    _migrate_dead_gemini_models(merged)
    if merged != raw:
        mw.addonManager.writeConfig(ADDON, merged)


def load_config() -> dict:
    raw = mw.addonManager.getConfig(ADDON) or {}
    cfg = _deep_merge(DEFAULTS, raw)
    _migrate_provider_schema(cfg)
    _migrate_creator_notetypes(cfg)
    _migrate_auto_tag_scheme(cfg)
    _migrate_dead_gemini_models(cfg)
    return cfg


def _as_model_dict(value, default_dict: dict) -> dict:
    """Coerce a model field (which may be a legacy string or already a dict)
    into a {family: model_id} dict, backfilling any missing family slots from
    `default_dict`. Idempotent."""
    if isinstance(value, dict):
        return {**default_dict, **{k: v for k, v in value.items() if v}}
    if isinstance(value, str) and value.strip():
        # Legacy single Claude model ID → seed the anthropic slot, keep the
        # other families on their defaults.
        return {**default_dict, "anthropic": value.strip()}
    return dict(default_dict)


def _migrate_provider_schema(cfg: dict) -> None:
    """Upgrade older single-provider configs to the multi-provider schema.
    Idempotent — runs every load_config but only mutates when legacy shapes
    are present. Mirrors _migrate_creator_notetypes."""
    # provider_mode ("auto"/"cli"/"api") → provider ("auto"/"cli"/"anthropic"/…).
    # Safe to map unconditionally: a legacy provider_mode only ever coexists with
    # the default provider ("auto"), so this never clobbers a real choice.
    legacy_mode = cfg.pop("provider_mode", None)
    if legacy_mode is not None:
        m = str(legacy_mode).lower()
        cfg["provider"] = {"api": "anthropic"}.get(m, m if m in ("auto", "cli", "anthropic", "gemini", "openai") else "auto")

    # model_default (string) → model_defaults["anthropic"]
    legacy_default = cfg.pop("model_default", None)
    if legacy_default and isinstance(legacy_default, str):
        cfg.setdefault("model_defaults", dict(_FAST_MODELS))["anthropic"] = legacy_default.strip()

    # Per-tool model fields: string → {family: id}
    tools = cfg.get("tools", {})
    qb = tools.get("qbank")
    if isinstance(qb, dict):
        qb["search_model"]   = _as_model_dict(qb.get("search_model"),   _FAST_MODELS)
        qb["card_gen_model"] = _as_model_dict(qb.get("card_gen_model"), _SMART_MODELS)
    for tool_key in ("browse", "gap_analyser", "card_creator"):
        t = tools.get(tool_key)
        if isinstance(t, dict):
            t["model"] = _as_model_dict(t.get("model"), _SMART_MODELS)


def _migrate_dead_gemini_models(cfg: dict) -> bool:
    """Rewrite stored Gemini model IDs that don't work on the free tier.

    Updating the add-on can't fix this on its own: Anki keeps the model IDs a
    profile already saved, so a profile first set up under old defaults still
    has gemini-2.0-flash / gemini-2.5-pro stored and keeps hitting 429 limit:0.

      • gemini-2.0-flash  → always rewritten (it lost its free tier entirely and
        2.5-flash supersedes it; no reason to keep it as a stored default).
      • gemini-2.5-pro    → rewritten ONCE, guarded by gemini_freetier_migrated,
        so free-tier users stop crashing on quota while paid users can pick Pro
        again later and keep it.

    Returns True if anything (including the guard flag) changed."""
    pro_done = bool(cfg.get("gemini_freetier_migrated"))
    dead = {"gemini-2.0-flash": "gemini-2.5-flash"}
    if not pro_done:
        dead["gemini-2.5-pro"] = "gemini-2.5-flash"

    changed = False

    def fix(holder: dict, key: str) -> None:
        nonlocal changed
        val = holder.get(key)
        if isinstance(val, dict):
            for fam, mid in list(val.items()):
                if mid in dead:
                    val[fam] = dead[mid]
                    changed = True
        elif isinstance(val, str) and val in dead:
            holder[key] = dead[val]
            changed = True

    fix(cfg, "model_defaults")
    tools = cfg.get("tools", {})
    qb = tools.get("qbank")
    if isinstance(qb, dict):
        fix(qb, "search_model")
        fix(qb, "card_gen_model")
    for tool_key in ("browse", "gap_analyser", "knowledge_gaps", "card_creator"):
        t = tools.get(tool_key)
        if isinstance(t, dict):
            fix(t, "model")

    if not pro_done:
        cfg["gemini_freetier_migrated"] = True
        changed = True
    return changed


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


def _migrate_auto_tag_scheme(cfg: dict) -> None:
    """Consolidate the old dual auto-tag system (per-type prefixes + a separate
    Browse prefix) onto the single {base}::{type}::… scheme. Idempotent: only
    rewrites a template still on the legacy default and only seeds a base when
    one is absent. Old per-type/Browse prefixes are left in place but unused."""
    kg = cfg.get("tools", {}).get("knowledge_gaps")
    if not isinstance(kg, dict):
        return
    legacy_tmpl = "{prefix}::{system}::{subsystem}::{topic}"
    if (kg.get("tag_scheme_template") or "").strip() == legacy_tmpl:
        kg["tag_scheme_template"] = _DEFAULT_TAG_TEMPLATE
    if not (kg.get("auto_tag_base") or "").strip():
        kg["auto_tag_base"] = "!!Fleg"


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


# ── provider / model resolution ────────────────────────────────────────────────

def family_for(provider: str) -> str:
    """Map a provider mode to its model family (anthropic/gemini/openai)."""
    return PROVIDER_OF.get((provider or "auto").lower(), "anthropic")


def active_family(cfg: dict | None = None) -> str:
    """The model family for the currently selected provider."""
    cfg = cfg if cfg is not None else load_config()
    return family_for(cfg.get("provider", "auto"))


def default_model_for(family: str, cfg: dict | None = None) -> str:
    """The configured default model for a family, falling back to built-ins."""
    cfg = cfg if cfg is not None else load_config()
    md = cfg.get("model_defaults") or {}
    return (md.get(family) or DEFAULTS["model_defaults"].get(family)
            or DEFAULTS["model_defaults"]["anthropic"])


def tool_model(tool_cfg: dict, field: str, family: str) -> str | None:
    """Return the model ID stored for `field` under the given family. Handles
    both the new {family: id} dict shape and a legacy string. Returns None when
    nothing usable is stored, so callers fall through to default_model_for()."""
    val = (tool_cfg or {}).get(field)
    if isinstance(val, dict):
        m = (val.get(family) or "").strip()
        if m:
            return m
        m = (val.get("anthropic") or "").strip()
        return m or None
    if isinstance(val, str) and val.strip():
        return val.strip()
    return None


_DEFAULT_TAG_TEMPLATE = "{base}::{type}::{system}::{subsystem}::{topic}"


def format_hierarchical_tag(prefix: str, levels: dict, template: str | None = None,
                            type_seg: str = "") -> str:
    """Render a hierarchical Anki tag from a base prefix, a type segment, and
    {system, subsystem, topic} levels, using `template` (defaults to the shared
    knowledge_gaps tag_scheme_template). Empty placeholders collapse away and
    each level is sanitised (no spaces/'::'/slashes). `prefix` fills both {base}
    and the legacy {prefix} alias. Shared by Create and Browse so the one tag
    scheme stays consistent."""
    import re as _re
    if template is None:
        try:
            template = (tool_config("knowledge_gaps").get("tag_scheme_template")
                        or _DEFAULT_TAG_TEMPLATE)
        except Exception:
            template = _DEFAULT_TAG_TEMPLATE
    levels = levels or {}
    sanitise = lambda s: _re.sub(r"[\s/]+", "_", _re.sub(r":+", "_", str(s))).strip("_")
    base = (prefix or "").strip().strip("_")
    safe = {
        "base":      base,
        "prefix":    base,  # back-compat alias for templates still using {prefix}
        "type":      sanitise(type_seg),
        "system":    sanitise(levels.get("system", "")),
        "subsystem": sanitise(levels.get("subsystem", "")),
        "topic":     sanitise(levels.get("topic", "")),
    }
    try:
        rendered = template.format(**safe)
    except (KeyError, IndexError):
        rendered = "::".join(p for p in (safe["base"], safe["type"], safe["system"],
                                         safe["subsystem"], safe["topic"]) if p)
    return _re.sub(r":{3,}", "::", rendered).strip(":")


def auto_tag_base() -> str:
    """The shared base prefix for auto-tags (e.g. '!!Fleg'). '' disables tagging."""
    try:
        return (tool_config("knowledge_gaps").get("auto_tag_base") or "").strip()
    except Exception:
        return ""


def kg_type_info(type_key: str) -> tuple[str, bool]:
    """Return (display_name, auto_tag_enabled) for a KG type key.
    Falls back to ('', False) when the type is unknown."""
    if not type_key:
        return "", False
    key = str(type_key).lower().strip()
    try:
        for t in tool_config("knowledge_gaps").get("types") or []:
            if isinstance(t, dict) and str(t.get("key", "")).lower() == key:
                return (t.get("name") or type_key), bool(t.get("auto_tag"))
    except Exception:
        pass
    return "", False


def month_tag() -> str:
    """The current month tag ("<prefix>::<YYYY-MM>"), or "" when the feature is
    off or has no prefix. Applied by Create and Browse for temporality."""
    cfg = load_config()
    if not cfg.get("month_tag_enabled", False):
        return ""
    prefix = (cfg.get("month_tag_prefix") or "").strip().strip(":")
    if not prefix:
        return ""
    from datetime import datetime
    return f"{prefix}::{datetime.now():%Y-%m}"


def set_window_geometry(width: int, height: int, x: int | None, y: int | None) -> None:
    cfg = load_config()
    cfg["window"] = {"width": int(width), "height": int(height), "x": x, "y": y}
    save_config(cfg)


def get_window_geometry() -> dict:
    return load_config().get("window", DEFAULTS["window"])
