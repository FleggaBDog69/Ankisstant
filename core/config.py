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
    "gemini":    ["gemini-3.5-flash", "gemini-2.5-pro", "gemini-2.5-flash", "gemini-2.0-flash"],
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
# gemini defaults to 3.5-flash for BOTH roles — 2.5-flash frequently returns
# quota/availability errors on the free tier, where 3.5-flash has been more
# reliable. Pro and the older 2.x models stay selectable in the picker for
# users who prefer them or are on a paid plan.
_FAST_MODELS:  dict[str, str] = {"anthropic": "claude-haiku-4-5-20251001", "gemini": "gemini-3.5-flash", "openai": "gpt-4o-mini", "ollama": "llama3.1"}
_SMART_MODELS: dict[str, str] = {"anthropic": "claude-sonnet-4-6",          "gemini": "gemini-3.5-flash", "openai": "gpt-4o",      "ollama": "llama3.1"}

# ── AI tool matrix ──────────────────────────────────────────────────────────────
# The canonical list of AI "tools" surfaced in the consolidated per-AI matrix on
# the Settings → AI page. Each tool has one model (and, for skill-capable tools, a
# skill) per provider family. This is the SINGLE place model choices live; the old
# scattered per-tool model keys (qbank.search_model, browse.model, …) survive only
# as optional per-tool OVERRIDES, read when their `<field>_override` flag is set.
# `tier` is the default suggestion ("fast" = cheap/search, "smart" = generation).
_AI_TOOLS: list[dict] = [
    {"key": "card_creation", "label": "Card creation",  "skills": True,  "tier": "smart"},
    {"key": "search",        "label": "Search / Browse", "skills": False, "tier": "smart"},
    {"key": "native_search", "label": "Native browser search", "skills": False, "tier": "smart"},
    {"key": "gap_analysis",  "label": "Gap analysis",    "skills": False, "tier": "smart"},
    {"key": "quality_pass",  "label": "Quality pass",    "skills": True,  "tier": "fast"},
]

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
    # Consolidated AI tool matrix (Settings → AI). The single source of truth for
    # which model (and skill) each AI tool uses, per provider family. Seeded from
    # the legacy per-tool model keys by _migrate_ai_matrix; thereafter the tool tabs
    # only override via their `<field>_override` flags. `skill` holds the family-
    # specific channel: `anthropic` = an Anthropic skill_id, `cli` = a CLI skill
    # invocation (e.g. "/malleus-anki"); other families ignore skills.
    "ai_matrix": {
        "card_creation": {"model": dict(_SMART_MODELS), "skill": {"anthropic": "", "cli": ""}},
        "search":        {"model": dict(_SMART_MODELS)},
        "gap_analysis":  {"model": dict(_SMART_MODELS)},
        "quality_pass":  {"model": dict(_FAST_MODELS),  "skill": {"anthropic": "", "cli": ""}},
    },
    # One-time seed of ai_matrix from the legacy scattered model keys. Flips True
    # after the first run so later AI-tab edits aren't clobbered by re-seeding.
    "ai_matrix_migrated": False,
    # Per-skill settings keyed by skill name (the bundled catalog skills:
    # anki-cards, anki-card-scorer, anki-browse). Currently just an optional
    # Anthropic custom skill_id for users who upload the skill to the Anthropic
    # API and want native by-id invocation. e.g. {"anki-cards": {"anthropic_skill_id": "skill_…"}}
    "skills": {},
    # Manual / paste provider: prebuilt "Ankisstant" assistants that bundle the
    # create/grade/browse instructions, so paste users send only the variable
    # content, never the full instructions. Shipped public links → every user gets
    # the "Open assistant" buttons. (Claude.ai has no shareable assistant link, so
    # web-Claude users copy the bundled Project instructions instead.)
    "manual_gpt_url": "https://chatgpt.com/g/g-6a258981479481918a8685bac4bae37c-ankisstant-custom-gpt",
    "manual_gem_url": "https://gemini.google.com/gem/1KPTwuU2mgZWmD7dDL5OiRN94CMTlGkkr?usp=sharing",
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
            # Weakness dashboard — aggregates captured missed questions (the MQ
            # KGs in kg_queue.json) by System/Subsystem/Topic inside the QBank
            # panel. weakness_window_days: 0 = all time.
            "show_weakness": True,
            "weakness_window_days": 30,
            "weakness_top_n": 8,
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
            # When true, the dedicated AI Browse panel is hidden from the
            # Ankisstant window and most of its settings tab is collapsed —
            # only the lightweight in-browser "✨ AI Search" checkbox (in
            # Anki's own Browse window) stays active. Set from the setup
            # wizard's "which tools?" page or Settings → Tools.
            "native_only": False,
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
            "auto_tag_base": "!Ankisstant",
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
                         "placeholder": "e.g. digoxin toxicity worsened by hypokalaemia",
                         "source": "capture", "surfaces": ["mq_capture", "home", "add_kg"],
                         "flows": ["create", "browse"],
                         "anki_target": {"kind": "role", "role": "missed_q", "position": 0, "mode": "append"}},
                        # AI-written teaching explanation of the missed concept,
                        # produced in the SAME generation call (see tools/kg/engine.py)
                        # and composed into the Missed Questions field above the stem.
                        {"key": "explanation", "label": "Explanation (AI)", "kind": "longtext",
                         "placeholder": "Filled by the AI — the mechanism behind the missed concept",
                         "source": "ai", "flows": ["create", "browse"], "cardinality": "note",
                         "ai_refs": ["concept", "stem_html"],
                         "ai_prompt": ("a brief teaching explanation (1-3 plain sentences) of the "
                                       "specific concept the student missed — explain the underlying "
                                       "mechanism or principle (the WHY/HOW), not just a restatement; "
                                       "be factually careful and don't invent specifics; pitch it at a "
                                       "Year 3 Australian medical student; plain prose, no markdown"),
                         "anki_target": {"kind": "role", "role": "missed_q", "position": 1, "mode": "append"}},
                        {"key": "stem_html", "label": "Question stem",   "kind": "html",
                         "placeholder": "Paste text or a screenshot (Cmd/Ctrl+V)",
                         "source": "capture", "surfaces": ["mq_capture"],
                         "anki_target": {"kind": "role", "role": "missed_q", "position": 2, "mode": "append"}},
                        {"key": "system",    "label": "System",          "kind": "text",
                         "placeholder": "e.g. Cardio"},
                        {"key": "subsystem", "label": "Subsystem",       "kind": "text",
                         "placeholder": "e.g. Arrhythmia"},
                        {"key": "topic",     "label": "Topic",           "kind": "text",
                         "placeholder": "e.g. Digoxin"},
                        {"key": "platform",  "label": "QBank source",    "kind": "text",
                         "placeholder": "e.g. AMBOSS, eMedici",
                         # Auto-filled from the QBank source on capture, so it's not
                         # an input on the capture popup — only on Home / Add-KG.
                         "source": "capture", "surfaces": ["home", "add_kg"]},
                        {"key": "notes",     "label": "Notes",           "kind": "longtext",
                         "placeholder": "Optional context, mnemonic, why you got it wrong",
                         "source": "capture", "surfaces": ["home", "add_kg"]},
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
            "auto_tag": True,
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
            # Background generation: when on, Generate enqueues a background job
            # (the user keeps using Anki) and finished cards land in a persistent
            # "ready to review" queue. Off restores the old modal generate→review.
            "background_generation": True,
            "max_parallel_jobs": 2,
            # Card Quality Pass: score generated cards against a rubric before
            # the review screen and act on the verdict (pass / flag / regenerate).
            # See tools/quality_pass.py. Off by default — opt-in.
            "quality_pass": {
                "enabled": False,                   # global master toggle
                # "separate_call" = one batch grade call after generation (can use
                #   a different/cheaper grader model); "same_call" = fold the grade
                #   into the generation call itself (self-grade, no extra round-trip).
                "grading_mode": "separate_call",
                # "flag_only" = chip + pre-fill the regen hint; "auto_regenerate" =
                #   silently regenerate failures (re-grading each attempt) up to
                #   auto_regen_max_retries before surfacing the best attempt flagged.
                "verdict_action": "flag_only",
                # Atomicity sensitivity: the max number of independent cloze
                # deletions allowed on one note before the pass flags it (D2).
                # 2 = a clean 2-cloze note passes; only 3+ independent facts flag.
                # Threaded into the deterministic prefilter, the LLM grader prompt
                # and the generator guidance so all three agree.
                "atomicity_max_clozes": 2,
                "auto_regen_max_retries": 2,
                # Auto-regen is suppressed for the manual/paste provider unless this
                # is on, since each retry is another copy/paste round-trip.
                "auto_regen_in_manual": False,
                # On Anthropic (auto/cli/anthropic), invoke the anki-card-scorer
                # skill instead of inlining the rubric, to save tokens. The bundled
                # inline rubric is always the fallback so grading works on every
                # provider (gemini/openai/ollama/manual).
                "prefer_skill": True,
                "grader_skill_invocation": "Use the anki-card-scorer skill",
                "grader_skill_id": "",
                # Per-family grader model. Grading is lighter than generation, so
                # this defaults to the cheaper "fast" models.
                "grader_model": dict(_FAST_MODELS),
            },
            # Source grounding: inject a curated Australian/WA clinical-guideline
            # citation allow-list into the generation prompt so cards cite real
            # URLs instead of hallucinated slugs. Topic-mode only (in source mode
            # the user's pasted material is the source). Resolution mirrors the
            # quality pass: global `enabled` here, an optional per-notetype
            # `grounding_override` (inherit/on/off), and a per-batch checkbox.
            "grounding": {
                "enabled": True,        # global default
                # Live fetch does blocking network I/O to pull guideline page
                # text into the prompt. Off by default — keeps generation snappy;
                # the allow-list alone already stops invented URLs.
                "fetch_live": False,
                # Shown in the injected citation-block header. Editable so the
                # registry is transferable across regions (e.g. "UK & NICE").
                "region_label": "Australian & WA",
                # Editable guideline registry. Empty → the built-in AU/WA defaults
                # in grounding/guidelines.py (BUILTIN_GUIDELINES) are used. Once the
                # user edits sources in Settings, the full list is stored here. Each
                # entry: {name, url, fetchable, specialties:[...]}.
                "sources": [],
            },
            # Online image search for the review screen. Auto-find fetches
            # candidate images only for cards the model flagged as visual (it
            # emits an `image_query`); manual per-card search/browse always work.
            "images": {
                "auto_find": False,        # per-batch "🖼️ Auto-image" default
                "max_per_card": 4,         # candidate thumbnails per visual card
                "sources": {
                    "wikipedia": True,
                    "openi": True,
                    "dermnet": True,
                    "radiopaedia": True,
                    "bing": False,         # only used when bing_api_key is set
                },
                # Azure "Bing Image Search" resource key. Blank → Bing stays off
                # even if its source toggle is on (it's a key-gated API, no scrape).
                "bing_api_key": "",
                # In-Anki visual browser: which image search the embedded webview
                # loads. "custom" uses browse_custom_url with {q} as the query slot.
                "browse_engine": "duckduckgo",   # duckduckgo | bing | google | custom
                "browse_custom_url": "",
            },
        },
        # Update by Tag: bulk-reprocess existing notes to the current format /
        # guidelines as new copies (originals untouched). Reuses the AI Create
        # notetype profiles + card-creation model; only its own tags live here.
        "update_by_tag": {
            "enabled": True,
            "audit_tag": "Ankisstant::AI::Updated",
            "review_tag": "Ankisstant::AI::UpdateReview",
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
    _migrate_ai_matrix(cfg)
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


def _migrate_ai_matrix(cfg: dict) -> None:
    """Seed the consolidated `ai_matrix` from the legacy scattered per-tool model
    keys, ONCE (guarded by `ai_matrix_migrated`). Mirrors _migrate_provider_schema.

    Each matrix tool takes its model from a PRIMARY legacy slot:
        card_creation ← card_creator.model
        search        ← browse.model
        gap_analysis  ← gap_analyser.model
        quality_pass  ← card_creator.quality_pass.grader_model
    SECONDARY slots that drive the same tool (qbank.search_model → search,
    qbank.card_gen_model → card_creation) are preserved exactly: if a secondary's
    stored model differs from the seeded matrix value for any family, its
    `<field>_override` flag is switched on so the tool keeps reading its own value.
    This makes the consolidation behaviour-preserving — nothing silently resets.

    Assumes _migrate_provider_schema already ran (legacy string models are now
    {family: id} dicts)."""
    if cfg.get("ai_matrix_migrated"):
        return
    tools = cfg.get("tools", {}) or {}
    matrix = cfg.setdefault("ai_matrix", {})

    def seed(tool_key: str, holder: dict | None, field: str, fallback: dict) -> dict:
        seeded = _as_model_dict((holder or {}).get(field), fallback)
        matrix.setdefault(tool_key, {})["model"] = seeded
        return seeded

    cc = tools.get("card_creator") if isinstance(tools.get("card_creator"), dict) else {}
    br = tools.get("browse") if isinstance(tools.get("browse"), dict) else {}
    ga = tools.get("gap_analyser") if isinstance(tools.get("gap_analyser"), dict) else {}
    qb = tools.get("qbank") if isinstance(tools.get("qbank"), dict) else {}
    qp = cc.get("quality_pass") if isinstance(cc.get("quality_pass"), dict) else {}

    card_seed   = seed("card_creation", cc, "model",        _SMART_MODELS)
    search_seed = seed("search",        br, "model",        _SMART_MODELS)
    seed("gap_analysis", ga, "model", _SMART_MODELS)
    seed("quality_pass", qp, "grader_model", _FAST_MODELS)

    # Seed the card-creation skill default from the legacy qbank card_skill_id.
    legacy_skill = (qb.get("card_skill_id") or "").strip() if isinstance(qb, dict) else ""
    if legacy_skill:
        matrix.setdefault("card_creation", {}).setdefault("skill", {})["anthropic"] = legacy_skill

    def preserve_override(holder: dict | None, field: str, seeded: dict, default: dict) -> None:
        """Turn on `<field>_override` when the holder's stored model differs from the
        matrix seed for any family, so the tool keeps its own model post-consolidation."""
        if not isinstance(holder, dict):
            return
        own = _as_model_dict(holder.get(field), default)
        if any(own.get(fam) != seeded.get(fam) for fam in own):
            holder[field + "_override"] = True

    preserve_override(qb, "search_model",   search_seed, _FAST_MODELS)
    preserve_override(qb, "card_gen_model", card_seed,   _SMART_MODELS)

    cfg["ai_matrix_migrated"] = True


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
    cc = tools.get("card_creator")
    if isinstance(cc, dict) and isinstance(cc.get("quality_pass"), dict):
        fix(cc["quality_pass"], "grader_model")

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
        kg["auto_tag_base"] = "!Ankisstant"


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


def ai_tools() -> list[dict]:
    """The canonical AI tool list for the consolidated matrix (Settings → AI)."""
    return [dict(t) for t in _AI_TOOLS]


def skill_anthropic_id(name: str, cfg: dict | None = None) -> str:
    """The optional Anthropic custom skill_id the user pasted for bundled skill
    `name` (for native by-id invocation on the Anthropic API). '' when unset."""
    cfg = cfg if cfg is not None else load_config()
    return str(((cfg.get("skills") or {}).get(name) or {}).get("anthropic_skill_id", "") or "")


def tool_model_for(tool_key: str, family: str | None = None, *,
                   cfg: dict | None = None,
                   override_cfg: dict | None = None,
                   override_field: str | None = None) -> str | None:
    """Resolve the model for an AI matrix tool, in order:
        1. per-tool OVERRIDE — `override_cfg[override_field]` when the tool has
           `override_cfg[override_field + "_override"]` truthy (hybrid: a tool tab
           may pin its own model);
        2. the consolidated `ai_matrix[tool_key].model` for the family;
        3. the family default (default_model_for).
    `family` defaults to the active provider's family. `cfg` is the full config
    (loaded if absent); `override_cfg` is the tool's own config slice."""
    cfg = cfg if cfg is not None else load_config()
    family = family or active_family(cfg)
    if override_cfg is not None and override_field and override_cfg.get(override_field + "_override"):
        m = tool_model(override_cfg, override_field, family)
        if m:
            return m
    matrix = (cfg.get("ai_matrix") or {}).get(tool_key) or {}
    m = tool_model(matrix, "model", family)
    if m:
        return m
    return default_model_for(family, cfg)


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


def quality_pass_cfg() -> dict:
    """The card-creator quality-pass config slice (toggle, mode, action, grader
    model/skill). Returns {} if unset. See tools/quality_pass.py."""
    try:
        return tool_config("card_creator").get("quality_pass", {}) or {}
    except Exception:
        return {}


def mq_explain_enabled() -> bool:
    """Whether missed-question cards carry an AI-written knowledge-gap
    explanation (leading the Missed Questions field, above the screenshot).
    Lives under qbank config; defaults on. Shared by Create and Browse."""
    try:
        return bool(tool_config("qbank").get("mq_explain", True))
    except Exception:
        return True


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
