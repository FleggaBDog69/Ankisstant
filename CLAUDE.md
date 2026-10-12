# Ankisstant — working notes for Claude Code

An Anki add-on (Python, Qt via `aqt`). This folder IS the live install —
`~/Documents/Projects/Ankisstant` is a symlink to it — so an edit here is what
Anki loads on its next restart. Anki has no hot reload: **every .py change
needs an Anki restart**, and Anki may be running while you work.

## Check your work

    python3 scratchpad/run_all.py          # lint + every offline test, ~5s

Run it after every change. A PostToolUse hook (`.claude/settings.json`) runs it
for you after edits to .py files and feeds failures back. What it covers:

- `scratchpad/tools/lint.py` — undefined names (pyflakes). Needs
  `scratchpad/tools/setup_dev.sh` once; skips cleanly without it.
- `scratchpad/test_imports.py` — every relative import and `module.attr`
  inside the add-on resolves. pyflakes can't see either, and a dead one stops
  the whole add-on loading. Deleting code? This is the test that matters.
- `test_*.py` — offline logic tests with `aqt` stubbed (see `harness.py`).
  None of them need Anki. Data tests use a temp folder, never `user_files/`.

Dead-code hunting: `python3 scratchpad/tools/lint.py --dead`, then
`python3 scratchpad/tools/cut.py FILE Name Class.method …` deletes whole
defs/classes (with their comment block) by name — safer than line numbers.

## See the UI

You can't click in Anki, but you can screenshot it while Anki is running with
the add-on enabled:

    touch user_files/dev_screens.request
    # within ~2s + ~20s of shots, user_files/dev_screens.done holds the folder
    cat user_files/dev_screens.done

Then Read the PNGs in that folder (`index.txt` lists them with sizes, and ends
with a measured PROBLEMS list: text wider than its widget, widgets past the
edge). Every page at the window's size and at 900×640, every Settings tab,
the Find & write plan page, and ~14 dialogs; `-full` shots are whole scroll
areas. It all happens OFF-SCREEN (WA_DontShowOnScreen) — nothing appears on
Fletcher's screen. Read-only: dialogs are cancelled, nothing is run.
`devscreens.py` reloads itself per request, so editing it needs no restart;
what it photographs does. A stuck run stops blocking new ones after 5 min, and
`index.txt` names the step it was on. Code: `tools/devscreens.py`.

Restarting Anki yourself: `osascript -e 'tell application "Anki" to quit'`
then `open -a Anki` — ask first, it's his running app. AnkiConnect is
installed: `curl -s localhost:8765 -X POST -d '{"action":"version","version":6}'`
answers once Anki is up (and tells you the main thread isn't hung).
`meta.json`'s `"disabled": true` is stale — the add-on loads regardless.

## Don't

- **Don't write `user_files/kg_queue.json` while Anki is open** — it's
  rewritten whole on the next save. Add gaps via `kg_inbox.json`, edit them via
  `kg_patches.json` (the `kg-add` skill documents both). `meta.json` holds API
  keys and Anki's copy of the config: don't print it, and don't edit it while
  Anki is running (Anki writes it back on quit).
- **Don't run LectureLens's `sync.py`.** `tools/lecture/` and `tools/pdf_render.py`
  are shared with the standalone LectureLens repo
  (`~/Documents/Projects/LectureLens-repo`), and this tree is ahead of its
  `shared/`. Syncing would overwrite work here. Avoid widening the drift in
  those files without a reason.
- **Don't treat these as dead code:** `tools/pipeline.py`'s stage contracts
  (deliberate scaffolding), the old results page in `tools/lecture/panel.py`
  (LectureLens's), and `tools/update_by_tag.py` (set aside — unreachable since
  phase 6, kept on purpose).
- **Qt `&`:** a single `&` in a button, menu or group-box label is eaten as a
  mnemonic — write `&&`. **Button widths:** the theme pads buttons 14px a side;
  never `setFixedWidth` a button — use `qt_utils.compact_button` for glyphs.
  Text that comes from data (group names, preset labels) needs `&` → `&&` too.
- **Button rows** that can get wide: `qt_utils.FlowLayout` (wraps) instead of
  a QHBoxLayout, whose minimum width is every button side by side.
- **Tables word-wrap by default** and break tags after "!!" — set
  `setWordWrap(False)` on any table showing tags.

## Shape of the thing

One run screen: **Find & write** (`tools/kg/bulk_search.py`, opened through
`open_unified` or `tools/run_view.open_run`). Every door — the Gaps page queue,
a group, AI Lecture (`tools/lecture/door.py`) — lands there. Its stages are
named in `tools/pipeline.py`; each AI stage's model comes from Settings → AI
(one row per stage, `pipeline.dial_for`). Pages: Knowledge Gaps (home),
AI Lecture, AI QBank. AI Browse, AI Create and Mass AI Create no longer exist;
`browse.py` survives only for the ✨ AI Search box in Anki's own browser.

Stores (`user_files/`): write through `core/jsonfile.py` — atomic, and a file
that won't parse is set aside rather than read as empty.

User-facing changes go in `CHANGELOG.md` under Unreleased. Commit on a branch;
`main` is what's released. Package with `./package.sh`.
