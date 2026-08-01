# The SynapsePro bridge — what was built, and the compromises in it

When [SynapsePro](https://github.com/mobesamedia/SynapsePro) is installed,
Ankisstant borrows its colours, puts its front door in SynapsePro's launcher
strip, and opens as a side panel beside SynapsePro's own — so the two read as one
app rather than two add-ons that happen to be installed together.

**None of it touches SynapsePro.** It's a fork whose changes go upstream as PRs,
and "let Ankisstant in" isn't a change upstream should have to carry. Everything
is done from the outside, every lookup is `try/except`-wrapped, and with
SynapsePro absent — or any kill switch off — Ankisstant looks and behaves exactly
as it did before this existed.

The general recipe and the trap list live in
`~/AnkiBlitz-repo/PORTING_TO_SYNAPSE.md`. This file is what's specific to
Ankisstant.

| Module | Job |
|---|---|
| `core/synapse.py` | Resolves SynapsePro; serves its palette as tokens, CSS vars and a Qt stylesheet |
| `core/synapse_sidebar.py` | The two buttons in its launcher strip |
| `ui/main_window.py` | The dock host, the icon rail, and the pop-out |

Everything is configured under the top-level `synapse` config block and
**Settings ▸ SynapsePro** (a tab that only appears when SynapsePro is detected).

## Where Ankisstant lives now

Two buttons go into SynapsePro's strip and no more: **Ankisstant** (accent
colour, opens the panel) and **＋** (text colour, adds a knowledge gap without
opening anything else). They go in **different places**, because they aren't the
same kind of thing:

- **Ankisstant** joins the feature group at the top, with the AI assistant and
  the mind map — it's a front door like they are. Inserted after the last button
  carrying `isMainIconButton`, found by that property rather than by index so a
  reordered strip upstream can't strand it.
- **＋** goes at the head of the bottom section, immediately under the
  `bottomSeparatorLine` rule and above the other add-ons' entries. It opens a
  dialog and nothing else, so it reads as a shortcut rather than a fourth app.

Both skip our own marked widgets when locating the anchor, or a re-injection at
profile open would chase itself down the strip. The per-tool icons stay on Ankisstant's *own* rail inside
the panel — a launcher strip with ten things in it is one nobody can read, and
SynapsePro's own features already own most of it.

Only the first is accented. A strip where everything is the accent colour has no
hierarchy left in it.

While those buttons are live, **the "Ankisstant" link in Anki's top toolbar
stands down** — the strip button is the same door. That check is made on every
toolbar redraw against whether the buttons are actually in the layout *right
now*, not against a flag set at startup: if injection ever fails, the link has to
still be there. The Tools menu entry, `Ctrl+Shift+L`, the Browse-window menu and
`Ctrl+M` are never touched.

## The dock, and the pop-out

`MainWindow` was a `QDialog` holding a nav sidebar and a stacked panel area.
It's now split three ways:

- **`AnkisstantBody`** — a plain `QWidget`: the whole UI, both session queues,
  and the queue API.
- **`MainWindow`** — the free-floating window, as before.
- **`AnkisstantDock`** — a `QDockWidget` on the right, placed through
  `constants.place_feature_dock` when SynapsePro offers it.

The module singleton `_current` points at the **body**, not the host. That's safe
because every consumer elsewhere is duck-typed on the queue API (`gap_queue`,
`browse_queue`, `refresh_queue_badge`, `refresh_tool_queue`, `show_create_tool`,
`show_browse_tool`) rather than on it being a window — the only two exceptions
were `open_main_window()` itself and `bulk_search.py`, which uses it as a dialog
parent, and any `QWidget` serves for that.

**Pop-out re-parents the body; it doesn't rebuild it.** That isn't an
optimisation. Each tool caches its panel in a module global
(`tools/browse.py::get_panel` and friends), so a rebuild would hand the same
widget to two parents. The pleasant side effect is that a half-filled Create form
and both queues survive the move. The one ordering rule: adoption has to happen
**before** the new body builds, because building selects a tool, which calls that
panel's `refresh_queue_state()` — against an empty queue that would clear the
form the pop-out is supposed to preserve.

**Several panels open at once needed no code at all.** SynapsePro's
`_handle_feature_click` doesn't close siblings, and Qt splits docks in one area
rather than tabbing them.

In dock mode the 200px text nav becomes a 44px icon rail, because a nav column a
third as wide as the panel is a nav column you resent. The popped-out window
keeps the text sidebar unchanged, and carries a **"Dock to side panel"** entry —
the wide sidebar is what you get *after* popping out, so without it the trip is
one-way.

**Panels are compacted to fit the dock** — `ui/compact.py`, once per panel, in
dock mode only. It is not a redesign: nothing changes what a page does, contains,
or the order of it. Four things overflow, always the same four:

1. **Hard minimum widths** (`setMinimumWidth(500)` on the form fields). A minimum
   is a floor the layout can't negotiate below, so instead of the field
   shrinking, the scroll area's contents get pushed past the viewport.
   Widgets where minimum == maximum are skipped — those are `setFixedWidth`
   icon buttons and label columns, sized to their own text, so squeezing them
   clips rather than reflows.
2. **Combo boxes sized to their longest entry.** "AnKingOverhaul (AnKing Step
   Deck / AnKingMed)" alone pushes a row off the panel, and because that width
   comes from its *contents*, lowering `minimumWidth` does nothing —
   `setMinimumContentsLength` is the lever. The drop-down list keeps its own
   width, so the full text is still readable where you read it.
3. **Long button and checkbox wording**, from a table keyed by caption; the full
   wording moves to the tooltip. Keyed by text rather than by widget because the
   alternative is a compact branch in every tool file, which is how a UI ends up
   with two of everything. Re-word a button upstream and it silently opts out —
   the failure is "not shortened", i.e. where we started.
4. **Margins and spacing**, repeated down a long form.

**Only the wording is undone on pop-out.** A panel is carried across rather than
rebuilt (the tools cache them), so without that the window would keep the dock's
abbreviations. The widths and margins are left alone: they were minimums and
paddings, and in a window twice as wide the layout stretches past both without
being told. The restore is skipped for any caption the tool has since changed
itself — a button mid-"Searching Anki…" is not ours to overwrite.

## Not by colour alone

The rail can't show `AI Create ●3` as label text, so a queued item draws a small
dot on the icon **and puts the count in the tooltip**. The dot means "something's
queued"; the number and the tool name are always readable in words. Same rule as
everywhere else in this add-on: the mark is a hint, never the state.

Detection status on the Settings tab is likewise **"SynapsePro detected" /
"not detected"** in words, not a green light.

## Token map

| Ankisstant's own | SynapsePro token | Where |
|---|---|---|
| `#4a90d9` new-card blue, lecture accents | `blue` | lecture results, `_c_new()` |
| `#3a9e6a` / `#3c8f5a` in-rotation green, "ok" | `green` | `_c_rot()`, `status_ok()` |
| `#c0392b` / `#c05050` error | `red` | `status_error()` |
| `#b85c00` / `#b8860b` warning, amber | `red` | `status_warn()` — see below |
| `rgba(80,160,255,…)` "queued" boxes | `blue_accent` at 0.16 / 0.55 / 0.30 | Create, Browse |
| `rgba(255,196,0,…)` setup banner | `red` at 0.18 / 0.65 | `make_setup_banner` |
| `palette(highlight)` nav + filter chips | `blue` | main window, KG chips |
| `palette(window)` / `palette(mid)` | `bg` / `grey_light` | nav sidebar, rail |

### Two deliberate compromises

1. **There is no amber token in SynapsePro's palette.** So `status_warn()` and
   `status_error()` both resolve to `red`, and warnings stop being visually
   distinct from errors while the bridge is on. Every one of those call sites
   also says which it is in words, which is why this is acceptable — a warning
   that differed from an error only by being slightly more orange was never much
   of a signal.
2. **The QBank heatmap's contribution ramp is not themed.** `#9be9a8 → #216e39`
   is a five-step sequential scale; SynapsePro has a single `green` token and no
   scale, and recolouring from one token would flatten the density read, which is
   the entire point of the chart. The heatmap also stays on Anki's `--canvas` and
   `--button-bg` for its surface: it's injected into Anki's own deck browser, not
   into a SynapsePro panel, so it should sit on that page rather than fight it.
   Only its borders pick up SynapsePro's.

Note also that mapping two accents onto `blue` and `blue_accent` would collapse
them under some of SynapsePro's colour themes — its themes rewrite the whole blue
family together. Where two things must stay distinct, one of them uses
`blue_bright`.

## Things that could go wrong, and don't

- **`palette()` is asked fresh on every call.** SynapsePro's active colour theme
  is module state it rewrites at runtime; a cached dict would go stale until the
  next Anki launch.
- **A colour-theme change is detected, not signalled.** `set_active_theme()`
  writes a module global and then repaints SynapsePro's *own* widgets by name.
  Nothing is emitted, and Anki's `theme_did_change` fires only for light/dark —
  so an already-open add-on window has nothing to subscribe to. `theme_signature()`
  is compared on window activation (and on the dock becoming visible), which is
  exactly when you'd get back from changing it. If SynapsePro ever grows a
  signal, delete that and use it. **This is the top item on the wish list for
  its author.**
- **No import-time colour constants.** `weakness._BAR_CSS` and the lecture
  panel's `_C_NEW` / `_C_ROT` were module constants and are now functions —
  as constants they'd have frozen the palette at Anki launch, and switching
  SynapsePro's theme would have appeared to do nothing until a restart. The
  offline harness greps for this.
- **Night state comes from `aqt.theme.theme_manager.night_mode`**, the *resolved*
  value. `mw.pm.night_mode()` — which SynapsePro itself uses — is only the stored
  preference and reads light while the OS is dark.
- **The Qt sheet is built through `_WithFallbacks`.** With `pal[key]` a single
  renamed token upstream would raise inside the f-string, collapse the whole
  sheet to `""`, and silently un-theme the window with nothing in the log.
- **Spin boxes and combos are all-or-nothing.** Styling the box is what makes Qt
  stop drawing its sub-controls, so if the arrow PNG render fails, *nothing*
  about those widgets is styled. Getting this half-right is easy and invisible —
  see below.
- **`gui_hooks` registration is guarded by a flag.** The sidebar re-injects on
  every profile open, but appending to the global hook lists each time would
  stack duplicate callbacks for the rest of the session.

## The bug the tests found

The first draft had `_combo_rules` correctly returning `""` when the arrow render
failed — but `QComboBox` was also named in the shared
`QLineEdit, QPlainTextEdit, QTextEdit, QComboBox { … }` rule, which wasn't
conditional. So the box got styled anyway, Qt dropped the chevron, and all 43
combo boxes would have looked like flat text fields with no sign they open.

**AnkiBlitz had the same bug**, inherited from the same sheet; it was fixed there
too (`engine/theme_bridge.py`), and the trap is now written into
`PORTING_TO_SYNAPSE.md`.

Nothing about it would have shown up by clicking around — the render succeeds on
a healthy machine. It took an offline harness with no Qt at all, where the render
*always* fails, to make the failure the default case.

## Not our config

Ankisstant reads SynapsePro's palette and **writes nothing to it**. Writing into
another add-on's config is the hard coupling the add-on-independence rule exists
to prevent.

Borrowing SynapsePro's AI provider and API keys was considered and dropped. It
would have worked — `ai_assistant._load_settings()` returns provider, model, key
and endpoints, read-only — but the two add-ons' AI paths don't otherwise touch,
and Ankisstant's per-tool model matrix would have had to be bypassed to use it.

## Deliberately not done

- **Any edit inside SynapsePro.** Not even to add an extension point.
- **Making the launcher strip auto-reveal for the Ankisstant dock mid-review.**
  `_feature_panel_dock_names()` is a hardcoded list inside SynapsePro, so it
  doesn't know about our dock. Not fixable from outside.
- **Theming `ui/welcome.py`** — 1,600 lines and ~28 colour sites, seen once.
- **Restyling checkbox and radio indicators.** A mis-specified `::indicator`
  reads as permanently unchecked, and with 56 checkboxes in Settings a window
  whose state you can't read is worse than one that doesn't quite match.
- **The KG type-colour swatches** (`ui/settings.py`) and the **bundled notetype
  CSS** (`tools/notetypes.py`) — those are user data and user content, not
  chrome. Changing the second would rewrite his notetypes.
- **Rebuilding any Ankisstant UI as HTML** to match SynapsePro's settings pages.
  The Qt stylesheet is the whole approach.

## If you're changing this

Run `scratchpad/test_synapse_bridge.py` (offline; stubs `aqt`, fakes a SynapsePro
theme module). It covers absent / disabled / broken SynapsePro, light vs dark, a
live theme switch, every kill switch, and four static sweeps: no import-time
colour constants, no import cycle through the bridge, no `theme_dialog` without
its import, and no `var(--ank-*)` without a fallback or with a name the bridge
never emits.

**The fake palette is four tokens on purpose.** Code that only works against a
complete palette breaks the first time upstream renames something. If a test here
fails and the fix looks like "add the missing token to the fake palette", the bug
is real and you've just hidden it.

The one manual check that isn't optional: **disable SynapsePro, restart, and
confirm Ankisstant looks and behaves exactly as it did before any of this —
including the top toolbar link coming back and the window opening free-floating
with its text sidebar.**
