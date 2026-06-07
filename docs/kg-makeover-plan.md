# Implementation plan — KG types declarative makeover

> Companion to `kg-types-audit.md` (Parts A & B). This is the agreed implementation
> plan, parked for later. Not yet started.

## Context
Today a KG type's *fields* are data-driven, but everything around them is hardcoded:
the AI prompts (`CARD_GEN_SYSTEM`, `AUTO_TAG_SYSTEM`, `MQ_EXPLANATION_SYSTEM`), which
behaviours fire (keyed on magic type keys `"mq"`/`"lo"` and field keys
`concept`/`stem_html`/`system`…), and how outputs land in Anki notes
(`ReviewDialog._build_note` role mapping). The single-AI-call optimisation is
implemented **twice** — `card_creator.py:_merged_gen_instructions` and
`browse.py:_merged_terms_instructions` are near-duplicates.

This makeover makes each field fully declarative: an **input source**, an optional
**AI prompt**, the **flows** it participates in, and an **Anki output target** — all
produced in **one AI call per flow** that works identically for every provider
including manual (copy/paste).

### Decisions locked with Fletcher
- **Full makeover** in one effort (sequenced internally for safety).
- **Dual target model**: a field can target either a **role** (resolved via notetype
  profile) **or** a **literal field name bound to a specific notetype**.
- **Editable prompts, advanced-only**: per-field AI prompt box, pre-filled with the
  tuned default + a Reset button; hidden behind an "Advanced" disclosure.
- **Unify Browse + Create** onto one engine, and add a per-field **flows** flag
  (`create` / `browse` / both) so e.g. an "Extra" field is generated only for Create,
  not Browse.

---

## 1. New data model — extended field spec

Each entry in a KG type's `fields[]` (config: `tools.knowledge_gaps.types[].fields`)
gains these keys (all optional, with behaviour-preserving defaults):

```jsonc
{
  "key": "explanation", "label": "Explanation", "kind": "longtext",  // existing
  "source": "ai",                 // NEW: capture | ai | capture+ai
  "surfaces": ["mq_capture"],     // NEW: input screens — mq_capture | home | add_kg
  "flows": ["create"],            // NEW: AI flows it participates in — create | browse
  "cardinality": "note",          // NEW: note (one value) | card (one per generated card)
  "ai_prompt": "<tuned default>", // NEW: only when source includes ai
  "ai_refs": ["concept","stem_html"], // NEW: other field keys fed into this prompt
  "anki_target": {                // NEW
    "kind": "role",               // role | field | tag | none
    "role": "missed_q",           // when kind=role: front|extra|image|one_by_one|missed_q
    "field": "",                  // when kind=field: literal Anki field name
    "notetype": "",               // when kind=field: which notetype this mapping is for
    "position": 1,                // ordering when several fields target one Anki field
    "mode": "append"              // append | prepend | replace
  }
}
```

Rules:
- `surfaces` only meaningful when `source` includes `capture`.
- `ai_prompt`/`ai_refs` only meaningful when `source` includes `ai`.
- `cardinality: card` is for per-card outputs (front/extra); `note` for one-per-note
  outputs (tag, explanation, MQ text). The engine keeps these separate in the request.
- `anki_target.kind = tag` is special — writes to `note.tags` via the existing
  hierarchical scheme, not a field.

Storage of KG *records* (`kg_queue.json` / `tools/kg/store.py`) is unchanged — it's
already a generic `fields{}` blob. All new metadata lives on the **type definition**
in config, not on records.

## 2. New module — `tools/kg/engine.py` (the unified one-call engine)

Replaces both `_merged_gen_instructions` and `_merged_terms_instructions`.

```
build_request(flow, type_meta, gap, base_system, base_user, want_cards|want_terms)
    → (system_prompt, user_prompt, plan)   # plan = which keys/cards expected back
dispatch(button, label, system, user, model, attachments)  → reply   # via run_claude_json
route(reply, plan, flow, gap, type_meta)   → RoutedResult
    .field_values   {field_key: value}     # per-note ai fields, persisted to KG store
    .cards          [ {role_key: value} ]   # per-card outputs
    .tag            "<hierarchical tag>"     # from a kind=tag target
    .note_plan      ordered segments per Anki target field   # for composition
```

- **build_request** gathers fields where `source ∈ {ai, capture+ai}` and
  `flow ∈ field.flows`; partitions by `cardinality`; emits a single JSON-OBJECT
  instruction whose keys are the field keys (plus `cards`/`terms`). Inlines `ai_refs`
  values as context. Generalises the existing object-reply trick verbatim, so manual
  mode stays one paste.
- **route** parses the object, distributes per-note values to KG-store fields (reusing
  `_persist_*` patterns), resolves the tag via `format_hierarchical_tag`, and builds a
  `note_plan`: for each Anki target field, an ordered (by `position`) list of segments
  to compose with each segment's `mode`.
- **Target resolution** (`resolve_target`): `kind=role` → look up the notetype
  profile field (`front_field`/`extra_field`/`image_field`/`one_by_one_field`, plus a
  new `missed_q` role → qbank `missed_q_field`); `kind=field` → literal field name,
  validated against the chosen notetype; `kind=tag` → tags.

## 3. Behaviour-preserving DEFAULTS (the migration's anchor)

In `core/config.py` `DEFAULTS[...]["knowledge_gaps"]["types"]`, fill the new keys so
**current behaviour is reproduced exactly** (regression guard):

- **mq type**: `concept` (capture; target role=missed_q pos0), `explanation`
  (ai; prompt = current `_MQ_EXPLANATION_GUIDANCE`; refs=[concept,stem_html];
  cardinality=note; flows=[create]; target role=missed_q pos1 append),
  `stem_html` (capture; surfaces=[mq_capture]; target role=missed_q pos2),
  `system/subsystem/topic` (capture+ai feeding the tag), plus a synthetic `tags`
  field (ai; prompt = current `AUTO_TAG_SYSTEM` rules; cardinality=note;
  flows=[create,browse]; target kind=tag).
  Front/extra are card-cardinality fields targeting roles front/extra.
- **kg / lo types**: notes (capture); lo keeps its analyser inputs.

Move the LO analyser and MQ-explain gating to read these flags instead of hardcoded
`type == "lo"` / `kg_type == "mq"` checks.

Add a migration (`core/config.py:_migrate_kg_field_specs`, run from `load_config`)
that upgrades any user type lacking the new keys: infer `source` from `kind`
(html/text/url/tag→capture), default `flows=[create]`, `cardinality=note`,
`anki_target.kind=none`; special-case known keys (`concept`,`explanation`,`tags`,
`stem_html`) to their roles. Mirrors existing `_migrate_creator_notetypes`.

Collapse the duplicate fields-less fallback in
`tools/knowledge_gaps.py:_load_types()` so it returns the DEFAULTS list (single
source of truth).

## 4. Rewire Create onto the engine

In `tools/card_creator.py`:
- Replace the `_merged_gen_instructions` block (~lines 1534–1596) with
  `engine.build_request(flow="create", …)` + `engine.route(...)`.
- Feed `route().note_plan` into `ReviewDialog` instead of the discrete
  `kg_concept/kg_explanation/kg_stem_html/kg_notes` params. `ReviewDialog._build_note`
  / `_enrich_extra` (~lines 2240–2410) become a generic "compose segments by
  position+mode into target field" loop. The hardcoded "Knowledge gap: … /
  explanation / stem / image" ordering becomes data (the mq defaults' positions).
- Keep the skill-flow fallback path (skills force a bare array — engine detects this
  and falls back to per-field separate calls, same as today's separate-call fallbacks).
- The BYO **Paste** path (`_on_paste_cards`) routes the pasted object through the same
  `engine.route`.

## 5. Rewire Browse onto the engine

In `tools/browse.py`: replace `_merged_terms_instructions` with
`engine.build_request(flow="browse", want_terms=True, …)`. Only fields whose `flows`
include `browse` are requested — so an "extra" field (flows=[create]) is silently
omitted from the Browse call. Terms + tag come back in the one object; apply
tag/unsuspend as today.

## 6. Generalise the capture surfaces

`tools/qbank/capture_dialog.py` currently hardcodes concept/stem/system/subsystem/
topic/platform/notes inputs. Rebuild it (and reuse on the home-screen capture +
Add-KG) to render inputs **from the active type's schema**, showing only fields where
`source` includes capture and `surfaces` includes the current screen
(`mq_capture` / `home` / `add_kg`). Reuse the existing `_build_field_widget` factory
from `tools/knowledge_gaps.py` (extract it to a shared helper).

## 7. Settings UI — extend the field/type editors

In `ui/settings.py:_FieldEditorDialog`, add:
- **Source** dropdown (capture / ai / capture+ai).
- **Show on** checkboxes (MQ capture / Home / Add-KG) — enabled when source has capture.
- **Used in** checkboxes (Create / Browse) → `flows`.
- **Cardinality** dropdown (Per note / Per card).
- **Anki output** editor: radio role-vs-literal; role dropdown OR (literal field name
  + notetype picker); position spinner; mode dropdown.
- **Advanced ▾** disclosure containing the **AI prompt** box (pre-filled with the
  field's default, **Reset** button) and an **AI inputs** multi-select (`ai_refs`,
  listing the type's other field keys).
Validate: ai fields need a prompt or a sensible default; literal targets must name a
field that exists on the chosen notetype.

## 8. Sequencing (single release, internally ordered to keep green)

1. Add the engine module + extended DEFAULTS + migration (no call sites changed yet) —
   addon still behaves exactly as before.
2. Rewire **Create** to the engine; verify equivalent cards/tags/MQ field across
   auto / api / manual providers.
3. Rewire **Browse** to the engine; verify terms + tag + flow-gating.
4. Generalise the **capture** surfaces from schema.
5. Ship the **settings UI** for the new attributes.
6. Remove dead code: `_merged_gen_instructions`, `_merged_terms_instructions`, the
   hardcoded `type=="lo"`/`kg_type=="mq"` branches, the duplicate `_load_types()` list.

## 9. Risks & mitigations
- **Output regression** — defaults reproduce current prompts verbatim; step 2/3 verify
  equivalence before any UI ships.
- **Broken user prompts / JSON** — engine `route` accepts partial objects (a missing
  key just skips that field) and reuses `_coerce_parsed_list`; manual mode reuses
  `manual_ai_complete(parse=…)` re-prompt-on-bad-paste.
- **Truncation** — reuse the dynamic token budget `min(8000, max(2000, n*120))`.
- **Cardinality bugs** — keep per-card vs per-note strictly separated in the request
  schema (the existing `{tags, mq_explanation, cards}` split is the template).
- **Literal targets vs notetype mismatch** — validate at edit time and again at create
  time; fall back to role if the named field is absent.

## 10. Verification
Manual end-to-end in Anki (load addon, MED profile):
- **Matrix**: providers {auto/CLI, api, manual} × flows {Create, Browse} × types {MQ,
  a new custom type made via Settings}.
- Confirm: exactly **one** AI call per generate (one paste in manual); per-note fields
  land in the right Anki fields; MQ field shows concept → explanation → stem → image in
  that order; tag applied via the hierarchical scheme; a `flows=[create]` field is
  absent from the Browse request; a literal-field target writes to the named field on
  its notetype; a role target resolves via the profile.
- Regression: existing KGs in `kg_queue.json` still open, edit, and create cards after
  the config migration.
- Syntax-check touched modules (no test suite in the addon).

## Files to change
- **New**: `tools/kg/engine.py` — assemble / dispatch / route / resolve_target / compose.
- `core/config.py` — extended type DEFAULTS, `_migrate_kg_field_specs`, role→field map.
- `tools/knowledge_gaps.py` — `_load_types()` single source; extract `_build_field_widget`.
- `tools/card_creator.py` — Create rewire; `ReviewDialog` generic segment composition.
- `tools/browse.py` — Browse rewire; flow-gated field selection.
- `tools/qbank/capture_dialog.py` — schema-driven capture inputs.
- `ui/settings.py` — `_FieldEditorDialog` new attributes + advanced prompt UI.
