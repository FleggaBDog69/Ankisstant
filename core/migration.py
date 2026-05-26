# One-shot migration from the three legacy addons.
# Runs once on first launch (flag is set in config so it never re-runs).
# Copies — never moves or deletes — so old addons keep working until the user
# removes them manually.

from __future__ import annotations

import os
import shutil

from aqt import mw

from .config import load_config, save_config

OLD_QBANK = "679076377"
OLD_BROWSE = "browse_with_claude"
OLD_CREATOR = "create_cards_with_claude"


def _get_old(addon_name: str) -> dict | None:
    try:
        cfg = mw.addonManager.getConfig(addon_name)
    except Exception:
        cfg = None
    return cfg if isinstance(cfg, dict) else None


def _addons_dir() -> str:
    """Return the addons21 directory. The new addon is one folder inside it."""
    here = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    return os.path.dirname(here)


def _user_files() -> str:
    here = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    path = os.path.join(here, "user_files")
    os.makedirs(path, exist_ok=True)
    return path


def _migrate_user_files() -> dict:
    """Copy queue / stats / cookie jars / session log from each old addon
    into the new user_files. Returns a small summary dict."""
    addons = _addons_dir()
    dst = _user_files()
    summary = {"queue": False, "stats": 0, "profiles": 0, "log": False}

    # QBank: missed_queue.json, stats*.json, profile_<key>/
    old_qb_uf = os.path.join(addons, OLD_QBANK, "user_files")
    if os.path.isdir(old_qb_uf):
        queue_src = os.path.join(old_qb_uf, "missed_queue.json")
        if os.path.isfile(queue_src):
            try:
                shutil.copy2(queue_src, os.path.join(dst, "missed_queue.json"))
                summary["queue"] = True
            except Exception as e:
                print(f"[ankisstant] copy missed_queue.json failed: {e}")
        for fname in os.listdir(old_qb_uf):
            full = os.path.join(old_qb_uf, fname)
            if os.path.isfile(full) and (fname == "stats.json" or fname.startswith("stats_")):
                try:
                    shutil.copy2(full, os.path.join(dst, fname))
                    summary["stats"] += 1
                except Exception as e:
                    print(f"[ankisstant] copy {fname} failed: {e}")
            elif os.path.isdir(full) and fname.startswith("profile_"):
                target = os.path.join(dst, fname)
                if not os.path.exists(target):
                    try:
                        shutil.copytree(full, target)
                        summary["profiles"] += 1
                    except Exception as e:
                        print(f"[ankisstant] copy {fname} failed: {e}")

    # Create Cards: card_creation_log.json
    old_cc_uf = os.path.join(addons, OLD_CREATOR, "user_files")
    log_src = os.path.join(old_cc_uf, "card_creation_log.json")
    if os.path.isfile(log_src):
        try:
            shutil.copy2(log_src, os.path.join(dst, "card_creation_log.json"))
            summary["log"] = True
        except Exception as e:
            print(f"[ankisstant] copy card_creation_log.json failed: {e}")

    return summary


def _pick_api_key(*candidates: str) -> str:
    for c in candidates:
        if c and c.strip():
            return c.strip()
    return ""


def _pick_cli_path(*candidates: str) -> str:
    for c in candidates:
        if c and c.strip():
            return c.strip()
    return ""


def _migrate_config() -> None:
    """Copy values from the three legacy configs into the merged one."""
    qb  = _get_old(OLD_QBANK)   or {}
    br  = _get_old(OLD_BROWSE)  or {}
    cc  = _get_old(OLD_CREATOR) or {}

    new = load_config()

    # Global keys
    new["anthropic_api_key"] = _pick_api_key(
        qb.get("anthropic_api_key", ""),
        br.get("anthropic_api_key", ""),
        cc.get("anthropic_api_key", ""),
    )
    new["claude_cli_path"] = _pick_cli_path(
        qb.get("claude_cli_path", ""),
        br.get("claude_cli_path", ""),
        cc.get("claude_cli_path", ""),
    )
    new["claude_cli_extra_args"] = (
        br.get("claude_cli_extra_args")
        or cc.get("claude_cli_extra_args")
        or new["claude_cli_extra_args"]
    )
    # The old browse/create used "provider" (cli|api). QBank used "card_gen_method"
    # (auto|cli|api). Prefer qbank's 3-way if present; otherwise map browse's 2-way.
    qb_method = (qb.get("card_gen_method") or "").lower()
    if qb_method in ("auto", "cli", "api"):
        new["provider"] = "anthropic" if qb_method == "api" else qb_method
    else:
        prov = (br.get("provider") or cc.get("provider") or "").lower()
        new["provider"] = {"api": "anthropic"}.get(prov, prov if prov == "cli" else "auto")
    legacy_default = (cc.get("model") or br.get("model") or "").strip()
    if legacy_default:
        new.setdefault("model_defaults", {})["anthropic"] = legacy_default

    # QBank section
    qb_section = new["tools"]["qbank"]
    if qb:
        for src_key, dst_key in [
            ("show_heatmap", "show_heatmap"),
            ("platforms", "platforms"),
            ("default_daily", "default_daily"),
            ("target_periods", "target_periods"),
            ("exam_dates", "exam_dates"),
            ("anthropic_model", "search_model"),
            ("anthropic_card_model", "card_gen_model"),
            ("card_skill_id", "card_skill_id"),
            ("card_prompt", "card_prompt"),
            ("card_notetype", "card_notetype"),
            ("card_deck", "card_deck"),
            ("missed_q_field", "missed_q_field"),
            ("tag_root", "tag_root"),
            ("last_system", "last_system"),
            ("last_subsystem", "last_subsystem"),
            ("last_topic", "last_topic"),
            ("ai_last_selected", "ai_last_selected"),
            ("ai_panel_visible", "ai_panel_visible"),
        ]:
            if src_key in qb:
                qb_section[dst_key] = qb[src_key]

    # Browse section
    br_section = new["tools"]["browse"]
    if br:
        for src_key, dst_key in [
            ("model", "model"),
            ("last_used_tag", "last_used_tag"),
            ("max_results", "max_results"),
            ("notetype_filter", "notetype_filter"),
            ("front_field", "front_field"),
            ("audit_tag", "audit_tag"),
            ("source_tags", "source_tags"),
        ]:
            if src_key in br:
                br_section[dst_key] = br[src_key]

    # Card Creator section
    cc_section = new["tools"]["card_creator"]
    if cc:
        for src_key, dst_key in [
            ("model", "model"),
            ("default_deck", "default_deck"),
            ("default_notetype", "default_notetype"),
            ("default_tags", "default_tags"),
            ("audit_tag", "audit_tag"),
            ("default_n_cards", "default_n_cards"),
            ("front_field", "front_field"),
            ("extra_field", "extra_field"),
            ("one_by_one_field", "one_by_one_field"),
            ("trust_mode_footer", "trust_mode_footer"),
        ]:
            if src_key in cc:
                cc_section[dst_key] = cc[src_key]

    save_config(new)


def _migrate_kg_queue() -> None:
    """Fold any legacy user_files/missed_queue.json into the unified KG
    queue. Idempotent — the store renames the legacy file when done."""
    try:
        from ..tools.kg import store as kg_store
        n = kg_store.migrate_from_missed_queue()
        if n:
            print(f"[ankisstant] migrated {n} missed-question item(s) into KG queue")
    except Exception as e:
        print(f"[ankisstant] KG migration failed: {e}")


def run_once_if_needed() -> None:
    """Idempotent — does nothing if already migrated."""
    cfg = load_config()

    if not cfg.get("migrated_v1"):
        print("[ankisstant] running first-launch migration…")
        try:
            _migrate_config()
            summary = _migrate_user_files()
            print(f"[ankisstant] migration done: {summary}")
        except Exception as e:
            # Don't trap the user in a broken state — mark migrated anyway so
            # we don't retry every launch. Failure details are in the console.
            print(f"[ankisstant] migration error (continuing): {e}")
        cfg = load_config()
        cfg["migrated_v1"] = True
        save_config(cfg)

    # KG queue unification (1.2.0). Runs on every launch but is a no-op once
    # the legacy file has been renamed by the store.
    if not cfg.get("migrated_kg_v1"):
        _migrate_kg_queue()
        cfg = load_config()
        cfg["migrated_kg_v1"] = True
        save_config(cfg)
