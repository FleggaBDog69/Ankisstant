# Changelog

## 1.6.5

### Fixed
- **All tools showed "Tool disabled — enable in Settings" after an AnkiWeb
  install.** The main window loaded each tool panel via a hardcoded
  `ankisstant.tools.*` module path. AnkiWeb names the add-on folder by its
  numeric ID, not `ankisstant`, so every panel import failed and fell back to
  the disabled placeholder — even though the tools were enabled in Settings.
  Local "Install from file" builds were unaffected (the folder is literally
  `ankisstant`), which is why it only surfaced on the first AnkiWeb download.
  Module paths are now derived from the package's real name at runtime.

## 1.6.4

### Fixed
- **Gemini free-tier defaults, for real this time.** Both the "fast" and "smart"
  Gemini defaults are now `gemini-2.5-flash` — the only model with free-tier
  quota. (Card generation previously defaulted to `gemini-2.5-pro`, which
  returns `429 limit: 0` on a free key — the "create over quota" crash.)
- **One-time migration heals already-saved configs.** Updating the add-on can't
  overwrite model IDs a profile already stored, so profiles set up under older
  defaults kept hitting 429. On load, stored `gemini-2.0-flash` / `gemini-2.5-pro`
  defaults are rewritten to `gemini-2.5-flash` (Pro once, guarded — paid users
  can re-select it and keep it).
- **Settings → Test connection no longer freezes/crashes Anki.** The Settings
  dialog is modal; its test ran a nested event loop inside it, which deadlocks on
  macOS. Now driven by `taskman` like the wizard test. This was provider-agnostic
  — it could crash on any provider, not just Gemini.
- Any unexpected error from an AI provider now degrades to a logged error +
  tooltip instead of potentially hard-crashing Anki from a background thread.
- Free-tier `429 limit: 0` errors now show an actionable message ("switch to
  Gemini 2.5 Flash") instead of a raw JSON dump.

### Added
- **Favourite question banks** step in the setup wizard: pick up to 3 from a
  curated list (UWorld, AMBOSS, Osmosis, eMedici, ClinicalKey, Passmedicine,
  Quesmed, Lecturio, Geeky Medics) to seed QBank's quick-launch buttons. More
  can be added/edited any time in Settings → QBank.

### Changed
- Model picker labels now state the free-tier reality (2.5 Flash = only free
  Gemini; 2.5 Pro = paid only; 2.0 Flash = legacy/no longer free).
- Docs note the confirmed-working providers: Gemini, Claude API, Claude CLI, and
  no-AI paste import. OpenAI and Ollama are implemented but not yet confirmed.

## 1.6

Documentation overhaul: all five providers now fully documented with per-path setup guides.
UI polish and usability improvements.

### Added
- `docs/setup-paste.md` — zero-friction paste import guide (any free chatbot)
- `docs/setup-gemini.md` — Gemini free tier setup (5 steps, no payment required)
- `docs/setup-ollama.md` — Ollama local AI setup guide
- `docs/setup-claude-cli.md` — Claude CLI and Anthropic API setup
- `docs/customising-cards.md` — card format customisation guide (system prompt override, skills, per-notetype profiles)
- Credits: Dr Patrick Lee (drpatricklee.substack.com) and Review Heatmap addon

### Changed
- README rewritten: outcome-first, provider-tier table, ChatGPT Plus ≠ API callout, Gemini privacy caveat
- ANKIWEB.md updated to reflect all providers; paste import path highlighted
- `manifest.json` description updated to reflect multi-provider support

## 1.5.1

Housekeeping: author/repo identity updated.

### Changed
- Author name updated to "Flegga" in `manifest.json`.
- Homepage URL updated to `https://github.com/FleggaBDog69/Ankisstant`.
- Stray name references in README and earlier changelog entries updated to "Flegga".

## 1.5.0

Sidebar reorder, **tag-search mode in Browse**, heatmap fixes, and a
**QBank picker** for the Practice Questions addon. All Analyse-KG
settings now live on the Knowledge Gaps tab.

### Added
- **Browse → Tags mode.** A new Notes / Tags toggle at the top of the
  Browse panel. In Tags mode, Claude returns `{keyword, resource, step}`
  triples for the topic; matching tags from your collection are listed
  with note-counts, the suggested resource (e.g. Boards & Beyond), and
  the step level. Double-click a tag row to open `tag:"X"` in the Anki
  browser. Confirm tags + unsuspends every note under the selected tags
  via the existing tag/unsuspend path.
- **Practice-Qs QBank attribution dialog.** When a Practice Questions
  session ends, a small picker asks which QBank it was from (lists every
  manual-logger profile, plus "Mixed / multiple QBanks", plus a "Don't
  log" button). Per-QBank picks consolidate into the matching
  `stats_<slug>.json`; Mixed routes to `stats_practice_questions.json`.
  Picker is skipped entirely when ankisstant isn't installed.

### Changed
- **Knowledge Gaps moved to the top of the Ankisstant sidebar.**
- **Analyse-KG settings folded into the Knowledge Gaps settings tab** as
  an "Analyse KG (AI sub-feature)" sub-group. The standalone Gap
  Analyser tab is gone; both `tools.knowledge_gaps` and
  `tools.gap_analyser` config namespaces continue to be written so no
  tool code had to change.

### Fixed
- **UWorld manual sessions now show on the heatmap.**
  `load_combined_stats` auto-discovers every `stats_*.json` in
  `user_files/` instead of iterating only the configured QBank
  platforms, so ad-hoc sources (UWorld, etc.) consolidate into the
  heatmap without being added to `config.platforms`.
- **"Open Practice Questions" button above the heatmap now works.**
  Added a `practice_questions:open` pycmd route that soft-imports
  `practice_questions.library.show_library`.

## 1.4.1

LO analyser is now embedded **inline on the LO-type KG detail pane** —
not a standalone page or dialog. Status filters simplified, screenshots
restored in the UI, and the post-search gap report retired.

### Added
- **Inline "Analyse this LO" section** on the KG detail pane, visible only
  when the active KG's type is `lo`. Reads the LO text + tag straight from
  the KG's schema fields, counts matched cards, and asks Claude what's
  missing. Accepted gaps are appended into this LO's own **Notes** field —
  no longer spun out as separate KG entries.

### Changed
- **Status simplified** to just **Open / Done / Dismissed**. The
  `in_progress` status is gone; legacy entries carrying it are coerced to
  `open` on read. Filter chips now match: Open / Done / Dismissed.
- **`Analyse LO…` dialog removed** from the KG page button row — replaced
  by the inline section above.
- **KG detail screenshot rendering fixed.** `_StemEdit` now sets a base URL
  pointing at Anki's media folder, so saved `<img src="qbank_capture_*.png">`
  references resolve. Previously-captured QBank items now show their stems
  in the UI without a re-capture.

### Removed
- **Browse → "post-search gap report"** retired (settings checkbox,
  Browse-side code path, and the `enable_gap_report` config key). The
  inline LO analyser fully covers the same workflow.

## 1.4.0

Per-type **custom field schemas** for Knowledge Gaps. Each type now owns
its own list of inputs — what the KG detail page renders is driven by the
active type's schema, not a fixed form.

### Added
- **Schema field on every type.** Each type has a `fields` list of
  `{key, label, kind, placeholder}`. `kind` can be:
    - `text` (single-line)
    - `longtext` (multi-line)
    - `html` (rich text + paste-screenshot, same editor QBank Capture uses)
    - `url` (with an Open button)
    - `tag` (single line with Anki tag autocomplete)
- **Default type schemas**:
    - **MQ** → concept, stem (html), system / subsystem / topic,
      QBank source, notes
    - **KG** → notes
    - **LO** → learning objective, anki tag, notes
- **Fields sub-editor** in Settings → Knowledge Gaps → Edit type.
  Add/edit/remove/reorder fields, with a live preview of the key + kind.
- **KG detail pane is now schema-driven.** Switching the type rebuilds the
  form, preserving values for overlapping field keys.
- **QBank Capture → KG** now writes into the MQ schema's fields blob, so
  screenshots and stems flow into the detail pane verbatim.
- **Send to Create** pulls supplemental context (stem + notes + concept +
  LO) from the type's fields instead of fixed top-level keys.

### Changed
- KG entries now store type-specific content under a `fields: {key: value}`
  dict on each entry. Legacy top-level keys (notes, stem_html, system,
  subsystem, topic, platform, lo, lo_tag) are auto-promoted into `fields`
  on first read — old entries continue to display without manual fixup.

## 1.3.0

Knowledge Gap **types** (MQ / KG / LO + custom), and completed items now
stay visible-but-greyed until you clear them.

### Added
- **`type` field on every KG.** Configurable list — defaults are
  **MQ** (Missed question, routed automatically from QBank captures),
  **KG** (generic knowledge gap, used for manual adds), and
  **LO** (learning objective, routed automatically from Analyse-LO results).
  Add / rename / recolour / delete types in Settings → Knowledge Gaps.
- **Type chips** below the status chips for one-click type filtering.
- **Type dropdown** in the ＋ Add KG modal and the per-KG detail pane.
- **Type editor** in Settings — name, hex colour (with native colour picker),
  and an optional description shown as the dropdown tooltip.
- **Clear completed** button on the KG page. Removes every Done / Dismissed
  KG with a confirm prompt. Counter updates live.

### Changed
- **Default filter is now "Active"** (open + in_progress + done). Completed
  items render greyed and sorted to the bottom so you can see what you've
  finished without it disappearing.
- **List items show a `[TYPE]` prefix** at-a-glance.
- QBank's "Review queue" / heatmap 📌 badge translate to **filter:type=mq**
  on the KG page.

## 1.2.0

Unified Knowledge Gaps queue: every "thing I don't know" — from Analyse KG,
QBank misses, and manual additions — flows through one persistent list.

### Added
- **Knowledge Gaps tab** (`tools/knowledge_gaps.py`) — persistent list +
  per-KG detail view. Each KG has title, tags, system/subsystem/topic, free
  notes, resource links (label + URL), an optional captured stem/screenshot,
  and a status (Open / In Progress / Done / Dismissed).
- **＋ Add KG button** — appears on the deck-browser home screen (next to
  the QBank heatmap) and in the Ankisstant sidebar. Opens a quick modal
  that adds a KG without leaving your current view.
- **Per-KG actions** — *Send to Browse with Claude* preloads Browse and
  marks the KG done on a successful tag/unsuspend. *Create card from this
  KG* hands the KG (with stem/notes) to Create with Claude; on a successful
  Add, the KG is auto-marked done.
- **Source filtering** — chips for Manual, Analyse, QBank, Browse plus
  status filters Open / In Progress / Done.
- **Knowledge Gaps settings tab** — toggle the home-screen button,
  delete-confirm prompt, and enable/disable the whole feature.

### Changed
- **Analyse Knowledge Gaps is now a sub-feature** of the new Knowledge Gaps
  tab — open it via *Analyse LO…* in the KG page sidebar. Approved gaps
  now flow into the persistent KG queue (not a session-scoped Create queue).
- **QBank's "Capture missed Q" + Review** now write into and read from the
  unified KG store. Captured items show up as **QBank-sourced KGs** in the
  Knowledge Gaps tab; the heatmap's 📌 badge still counts them. Stem
  screenshots are preserved verbatim.
- **MainWindow.gap_queue** items are now dicts (`{title, kg_id, stem_html,
  notes}`) — Create gets richer context, and KG handoff is round-tripped
  via `kg_id` to mark Done on success.

### Migration
- One-shot: any existing `user_files/missed_queue.json` items are converted
  to source=qbank KGs on first 1.2.0 launch. The legacy file is renamed to
  `missed_queue.migrated.json` (kept as a backup).

## 1.1.0

Hardening pass before sharing the addon outside Flegga's profile.

### Added
- First-launch **welcome wizard** (`ui/welcome.py`) — pick provider, test
  connection, pre-fill default deck / notetype / audit-tag prefix.
- **Setup banner** on every tool panel when no Claude provider is configured.
- **About tab** in Settings — version, no-telemetry note, button to re-run
  the welcome wizard.
- **Help buttons** (`?`) on every tool panel with workflow-specific docs.
- **PDF / PPTX attachments** in Create with Claude. PDFs go to Claude (CLI
  Read tool or API document block); PPTX text is extracted locally from the
  OOXML (slides + speaker notes).
- **PDF size guards** — confirm prompt at >10 MB, refused at >30 MB.
  PPTX with no extractable text now warns the user instead of silently
  sending an empty source.
- **Add Cards auto-advance queue** — opening *Open Add screen* from the
  review dialog now keeps the queue alive across Anki's "Add" clicks via
  `gui_hooks.add_cards_did_add_note`. Closing the Add window stops it.
- **Notetype / field validation** — Create with Claude and QBank now check
  that the configured notetype exists *and* has the expected fields before
  opening the Add Cards dialog.
- **Setup-error dialog** — when no Claude backend is configured, errors
  surface as a modal with an "Open setup…" button instead of a tooltip.
- **Thin logger** (`core/log.py`) honouring `debug_logging` config flag.
- `core/anki_utils.require_col()` helper, wired into every user-triggered
  handler that touches `mw.col`.
- `NOTICES.md` documenting the AGPL v3 ported code.
- `README.md` with install, setup, day-to-day, and troubleshooting sections.

### Changed
- **Tag input** on Create with Claude is now 3× wider (visual overflow fix).
- **Cross-platform CLI detection** — added Windows fallback paths
  (`AppData\Roaming\npm`, scoop shims, Program Files) and `claude.cmd` /
  `claude.exe` lookup alongside `claude`. Added Volta / Bun / `n` paths on
  POSIX.
- **Config defaults sanitised** — removed personal tags / notetypes
  (`!!Curtanki::*`, `!!Fleg::*`, `AnKingOverhaul`, `!Medicine::!Content`,
  `Kush_*`, `Malleus`, `#AK_*`). New defaults are empty strings the welcome
  wizard fills in.

### Removed
- **AJT Card Management dependency.** `grade_cards()` is now ported in-tree
  at `tools/qbank/grade_cards.py` (AGPL v3 attribution in `NOTICES.md`).
  Users no longer need to install AnkiWeb 1021636467 separately.

### Fixed
- Tag chip suffix-only rendering: defensive `editor.tags.setText` after
  `set_note` in the Add Cards auto-fill path.
- Progress-dialog race RuntimeError when a Browser open + immediate
  `search_for` triggered two overlapping progress dialogs.

## 1.0.0

Initial merge of QBank, Browse, and Create-with-Claude addons into a single
Ankisstant suite.
