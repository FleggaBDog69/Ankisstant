# KG Types — architecture audit

> Reference for a planned larger-scale change to how Ankisstant handles Knowledge
> Gap ("KG") *types*. Audits the current design: how types are defined, how KG data
> is stored/written/read, whether it's standardised, whether a non-coder can add a
> new type unaided, and how types relate to AI providers (including the copy/paste
> "manual" provider).

## 1. What a "KG type" is

A KG type is a **config-defined dict**, not code. Defined in
`core/config.py` → `DEFAULTS["tools"]["knowledge_gaps"]["types"]`, each is:

```
{ key, name, color, description, auto_tag: bool,
  fields: [ {key, label, kind, placeholder}, ... ] }
```

- `kind` ∈ `text | longtext | html | url | tag` — the only 5 widget kinds
  (`tools/knowledge_gaps.py:_build_field_widget`, `ui/settings.py:FIELD_KIND_LABELS`).
- Factory types: **mq** (concept, stem_html, system, subsystem, topic, platform,
  notes; auto_tag on), **kg** (just notes; auto_tag on), **lo** (auto_tag off).
- Users add/edit/delete/reorder types and fields entirely through
  **Settings → Knowledge Gaps** via `_TypeEditorDialog` + `_FieldEditorDialog`
  (`ui/settings.py:1250+` / `1189+`).

## 2. Storage / write / read — IS standardised

One flat JSON list: `user_files/kg_queue.json`. Every KG of every type shares the
**same record shape** (`tools/kg/store.py:_normalise`):

```
{ id, title, source, type, status, dismissed, tags[], fields{}, resources[],
  created_at, updated_at }
```

- **Type-specific content lives in the generic `fields{}` dict** (key→string, or a
  list/dict kept as-is). This is the standardised, type-agnostic storage contract.
  `add / add_many / update / remove / _normalise` have zero per-type logic.
- The detail pane **renders widgets dynamically from the active type's schema**
  (`KGDetailPane._rebuild_schema_for_type` + `_build_field_widget`); getters/setters
  round-trip values into `fields{}`. Switching a KG's type re-renders the form and
  preserves overlapping keys (`_on_type_changed`, `_collect_schema_fields`).
- Legacy top-level keys (notes, stem_html, system…) auto-promote into `fields{}` on
  read (`LEGACY_FIELD_KEYS`); `store.field()` reads either location.

**Verdict: storage and the CRUD/render path are fully standardised and generic.**

## 3. Can a user make a new type without coding/AI? — YES (definition), NO (behaviour)

Adding a type is a pure GUI flow (name, colour, description, auto-tag toggle, then
add fields by label + kind + placeholder). No JSON, no code — a non-coder can do it.

**But the generic storage hides that many consumers hardcode specific field/type
keys.** A user-made type's fields store and display fine, but won't *participate* in
behaviours unless they reuse the "magic" keys other code expects:

- **QBank capture** writes fixed `concept, stem_html, system, subsystem, topic,
  platform, notes` and is hardwired to `type="mq"`.
- **Send → Create** (`knowledge_gaps.py:_send_to_create`) hardcodes pulling
  `stem_html, notes, lo, concept, explanation, images`.
- **MQ explanation** (`card_creator.py:_should_explain_gap`) keys off
  `kg_type == "mq"` and reads `concept`/`stem_html`.
- **LO analyser** (`_LOAnalyserSection`) only shows for `type == "lo"`; reads
  `lo, lo_tag, notes`.
- **Auto-tag** reads `system / subsystem / topic` + the per-type `auto_tag` flag.

So a user can make the type, but cannot make it behave like MQ/LO — those behaviours
are bound to hardcoded type/field keys, not data-driven. The settings UI even warns
not to rename MQ default keys. **This implicit field-key contract is the single
biggest fragility for any larger-scale redesign.**

## 4. Latent issue — two divergent default sources

- `core/config.py DEFAULTS` has the full type list **with** fields.
- `tools/knowledge_gaps.py:_load_types()` has a **second** hardcoded fallback
  (mq/kg/lo) **without** fields, used only if the config list is wiped. The two can
  drift; hitting the fallback silently yields field-less types.

## 5. Type vs source — different governance

- `type` is **free-form + user-configurable** (any slug).
- `source` is a **fixed enum** `{manual, analyse, qbank, browse}`
  (`store.VALID_SOURCES`). Worth deciding deliberately if the model is reworked.

## 6. KG types ↔ providers — orthogonal and standardised

Provider is a **single global setting** `cfg["provider"]`:
`auto | cli | anthropic | gemini | openai | ollama | manual`.

- **All AI calls funnel through one dispatch layer**: tools call
  `qt_utils.run_claude_json / run_claude_text` → `core/api.py:ask_claude /
  ask_claude_json`, which branches per provider (`_call_cli`, `_call_api`,
  `_call_gemini`, `_call_openai`, `_call_ollama`, manual).
- **Nothing in a KG type references a provider, and nothing in the provider layer
  references KG types.** Fully decoupled. Every type's actions (Create, Browse,
  Analyse, bulk-import-from-PDF) use the same dispatch; provider is chosen globally,
  never per-type. Provider behaviour is uniform across all types.

### Copy-and-paste = the `manual` provider — first-class at dispatch
- `ask_claude` checks `provider == "manual"` early and routes to
  `qt_utils.manual_ai_complete()`: assembles `[SYSTEM]+[YOUR TASK]`, shows it for the
  user to copy into any external LLM, and collects the pasted reply (with optional
  `parse` callback for JSON, re-prompting on bad paste).
- `run_claude_*` detect `is_manual_provider()` and run **synchronously on the main
  thread** (no background worker / progress dialog) so the dialog can show.
- BYO/no-sub AI is implemented as a real provider at the dispatch layer, not a
  bolt-on — so it works for every KG type automatically.

### Provider capability gaps (not type-specific)
- **Attachments** (PDF bulk-import, KG images): `manual` can't send files — it warns
  the user to upload them to their own chat. Degradation applies to all types equally.
- **Skills** (`card_skill_id`): only honoured on `cli`/`anthropic`; ignored elsewhere.

## 7. Summary

| Question | Answer |
|---|---|
| Is KG storage standardised? | **Yes** — one record shape, generic `fields{}`, type-agnostic store. |
| Is the type definition standardised? | **Yes** — config dict with a uniform field-schema. |
| Can a user add a type unaided? | **Yes** for definition/UI; **No** for behaviour (MQ/LO logic hardcoded to magic keys). |
| Is type↔provider interaction standardised? | **Yes** — orthogonal; one global provider, one dispatch layer for all types. |
| Is copy/paste a first-class provider? | **Yes** — `manual` lives at the dispatch layer like every other provider. |
| Main fragility for a redesign | Implicit hardcoded field-key + type-key contract across qbank/create/LO/auto-tag; plus the duplicate fields-less default list in `_load_types()`. |

## 8. Files to know (for the eventual change)
- `tools/kg/store.py` — persistence, normalisation, the field contract.
- `tools/knowledge_gaps.py` — panel, dynamic schema rendering, Create/Browse handoff (`_send_to_create`, `_LOAnalyserSection`).
- `core/config.py` — `DEFAULTS[...]["knowledge_gaps"]["types"]`, `kg_type_info`, tag-scheme helpers.
- `ui/settings.py` — `_TypeEditorDialog` / `_FieldEditorDialog`, `FIELD_KIND_LABELS`.
- `core/api.py` + `core/qt_utils.py` — provider dispatch, `manual_ai_complete`, `is_manual_provider`.
- `tools/card_creator.py` — MQ-explain + auto-tag generation keyed on type/field keys.

---

# Part B — Audit of the proposed KG-types makeover

## The idea, restated
Make each KG type fully declarative. Each **field** on a type carries:
1. an **input source** — where it's captured (MQ capture, home KG capture, Add-KG),
2. an optional **AI prompt** — what the AI should produce for that field,
3. an **Anki output target** — which Anki note field it writes to.

All AI-backed fields are produced in **one AI call** (tag + explanation + search
terms + front + extra together), working identically for every provider including
the no-provider copy/paste ("manual") setting.

Example (MQ capture): captured stem → MQ "question stem" field → Anki "Missed
Questions" field; stated specific KG → "specific KG" field → AI (return KG +
explanation) → "Missed Questions" field above the image; one call returns tag +
explanation + search terms.

## B1. How much already exists (the idea is well-aligned)
Not from-scratch — the code already takes two steps toward it, hardcoded/partial.

| Idea component | Current state |
|---|---|
| Switching type changes fields shown | **Exists** — `KGDetailPane._rebuild_schema_for_type`. |
| Editable per-type field schema | **Exists** — `_TypeEditorDialog`/`_FieldEditorDialog`. |
| One AI call for tag + explanation + cards | **Exists** — `card_creator.py:_merged_gen_instructions` folds them into one JSON-object reply, *explicitly so manual/BYO is one paste, not three*. Separate-call fallbacks only for skill flows. |
| Output → Anki field mapping | **Exists but coarse** — notetype-profile roles (`front_field`/`extra_field`/`image_field`/`one_by_one_field`), not per-KG-field, not user-attachable. |
| Provider-agnostic incl. manual | **Exists** — one dispatch layer; `manual_ai_complete(parse=…)` handles paste-back + re-prompt. |

The single-call goal is therefore **already validated as workable**.

## B2. What the makeover really is
Today there are **three disjoint systems**: input schema (data-driven `fields[]`),
AI prompts (hardcoded module strings bound to magic keys `"mq"`/`"lo"`/`concept`/…),
and output mapping (notetype-profile roles). The idea **collapses all three into one
field spec** — which directly removes the hardcoded-key fragility flagged in Part A
§3. That's the strongest argument for the makeover.

## B3. Design tensions to resolve (the substance)
1. **Heterogeneous field roles** → need an explicit **mode**: `capture | ai | capture+ai`.
2. **"Prompt per field" + "one call" reconcile only via assembly**: gather all `ai`
   fields → one JSON-object request keyed by field key → parse → route. This
   generalises `_merged_gen_instructions` to N fields and is **the core new engine**.
3. **AI fields depend on other fields** → need **`ai_refs`** (which field values feed
   a prompt; explanation needs `concept`+`stem`).
4. **Cardinality mismatch**: `front`/`extra` are *per-card* (N cards);
   `tags`/`explanation`/MQ are *per-note*. Current call already separates `cards:[…]`
   from `tags`/`mq_explanation` — preserve this; tag each ai-field `note` or `card`.
5. **Many fields → one Anki field, ordered** (explanation above image) → target needs
   **position + compose mode** (`append/prepend/replace`), not just a name.
6. **Tags aren't a field** — they write to `note.tags` via
   `{base}::{type}::{system}::{subsystem}::{topic}`; keep "tag" a special target type
   reusing `format_hierarchical_tag`.
7. **Images can't ride the JSON call** (degrade on manual) → capture→passthrough,
   routed straight to the target, outside the AI batch.
8. **Targets are notetype-relative** → decide: literal field names per
   KG-type×notetype, or roles resolved by existing `notetypes` profiles (lower
   friction).
9. **Capture surfaces** → per-field `surfaces: [mq_capture, home, add_kg]` flag;
   storage is already unified, so this is presentation-only.
10. **Search terms** are a separate Browse flow today → folding them into the one call
    means Browse + Create share the assembler (feasible; both use `run_claude_*`).

## B4. Risks
- Over-configuration / broken prompts → need tuned defaults, "reset to default",
  resilient partial-object parsing.
- Quality regression → ship the existing tuned prompts (e.g. `AUTO_TAG_SYSTEM`) as
  defaults, don't replace with generic ones.
- Batch truncation → reuse the dynamic token budget `min(8000, max(2000, n*120))`.
- Manual payload size → one larger prompt, but **one paste not three** = net win.
- Migration → map legacy magic-key KGs; collapse the duplicate `_load_types()`
  fallback into config DEFAULTS.

## B5. Minimal viable field-spec shape
```jsonc
{
  "key": "explanation", "label": "Explanation", "kind": "longtext",
  "source": "ai",                       // capture | ai | capture+ai
  "surfaces": ["mq_capture"],
  "ai_prompt": "<default tuned prompt>",
  "ai_refs": ["concept", "stem_html"],
  "cardinality": "note",                // note | card
  "anki_target": { "field": "Missed Questions", "position": 0, "mode": "prepend" }
}
```
Plus one **batch engine** generalising `_merged_gen_instructions`: collect `ai`
fields → one JSON-object request → parse (reuse `_coerce_parsed_list` + object
handling) → route to fields + Anki targets; manual uses a single
`manual_ai_complete(parse=…)`.

Phasing: (1) generalise the single-call engine to N declarative ai-fields, current
behaviour as default config (no UX change); (2) per-field `anki_target` via notetype
profiles; (3) `source`/`surfaces` to unify the three capture screens; (4) migrate
hardcoded `mq`/`lo` behaviours into defaults + collapse the duplicate default list.

## B6. Verdict
Coherent and well-targeted: it generalises two mechanisms the code already reaches
for (single merged call; output mapping) and kills the hardcoded-key fragility. The
hard parts are the **data-model decisions** in B3 — especially field *mode*,
per-card vs per-note *cardinality*, and many→one *target ordering*. Nail those three
and the rest is a straightforward generalisation of existing code.
