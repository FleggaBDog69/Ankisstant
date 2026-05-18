# Changelog

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

Hardening pass before sharing the addon outside Fletcher's profile.

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
