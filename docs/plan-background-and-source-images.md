# Plan — true background generation + images from the source

Status: **awaiting sign-off**. Nothing in here is built yet. The separate fixes
already landed (see "Already done" at the bottom) are listed so this plan doesn't
re-litigate them.

Two independent workstreams. They touch different files and can ship in either
order, but both are written up together because they share one idea: the Create
panel should hand work off instead of doing it itself, and the source you
uploaded should be treated as a first-class asset, not just prompt text.

---

## Workstream A — make "background" actually mean background

### A0. What's wrong

Three separate defects wear the same symptom ("I pressed Go and Anki froze").

**A0.1 — the runner was on the wrong executor.** *(fixed — see Already done)*

**A0.2 — Go does the heavy lifting before it enqueues.**
`card_creator._on_generate` (`tools/card_creator.py:2394`) runs the entire
pre-flight synchronously on the UI thread:

| step | line | cost |
|---|---|---|
| `_fetch_url` | `:2446` | network, 12s timeout × retries |
| `_extract_pptx_text` | `:2476` | zip + XML parse per slide |
| `extract_pdf_text_per_page` | `:2516` | whole document |
| `render_pdf_to_images` | `:2529`, `:2537` | **up to 80 pages at 1280px** |
| `copy_attachments` | `:3249`, `:3253` | copies every rendered PNG into `user_files/` |

`loading()` (`core/qt_utils.py:25`) calls `processEvents()` once on entry and
then blocks, so all of that is a hard freeze. The job is only enqueued at
`:2787`, *after* every one of those steps. Fixing A0.1 does not help here at all.

**A0.3 — Regenerate / Split / One-by-one were never backgrounded.**
`_on_regen_clicked` (`:3669`) calls `_regenerate_one` (`:957`) → `ask_claude_json`
directly inside `loading()`. Same for `_on_obo_clicked` (`:3685`) and
`_on_split_clicked` (`:3681`). These are full AI calls on the main thread with no
progress dialog and no cancel. This is the one you hit on the Add screen.

### A1. Design — a `preparing` job phase

Move the boundary: **Go enqueues immediately**, and everything above becomes the
job's first phase rather than a precondition for creating it.

Add `PREPARING` to the status set in `tools/create_jobs.py:79`. A job's life
becomes `queued → preparing → running → ready`. The panel's queue list already
renders arbitrary statuses, so it gets a "Preparing…" row for free.

Split the pre-flight into two buckets by whether it needs Qt:

- **Qt-free → the worker thread** (`_worker`, `create_jobs.py:355`): URL fetch,
  PPTX text extract, PDF *text* extract, attachment copying. All of it is pure
  I/O and already safe off-thread.
- **Qt-bound → the main thread, chunked**: only `render_pdf_to_images`. QtPdf
  wants the GUI thread, so instead of moving it, break it up: render one page per
  `mw.taskman.run_on_main` hop and let the event loop breathe between pages. An
  80-page deck becomes 80 × ~40ms slices instead of one 3-second block, and the
  job reports `Preparing — slide 12/80`.

`pdf_render.render_pdf_to_images` gains a generator sibling
`iter_pdf_pages(path)` that yields one rendered page at a time; the existing
function becomes `list(iter_pdf_pages(...))` so nothing else changes. The
existing `_CACHE` keeps working — a deck already rendered for the panel preview
won't re-render for the job.

**Validation stays on the main thread, before enqueue.** "No notetype selected",
"provide text or attach a file", the >25-slide confirmation — those are cheap and
must stay interactive. Only the expensive I/O moves. This is the important line:
Go should still refuse a malformed request instantly, and only defer real work.

### A2. Regenerate / Split / One-by-one

Route all three through the existing `run_claude_json` helper
(`core/qt_utils.py:299`), which already gives a cancellable modal and a nested
event loop that keeps the app responsive.

`_regenerate_one` is shared with the quality-pass auto-regen loop
(`card_creator.py:3042`), which runs from a context where a modal is wrong. So
split it: `_regenerate_one` stays a pure, Qt-free function (the loop keeps
calling it directly), and only the *button* handlers wrap it in
`run_claude_json`. Same shape for `_on_one_by_one`.

Deliberately **not** using the full job queue for these. A regenerate is a
one-card, few-second, in-dialog edit — you're staring at the row waiting for it.
A cancellable modal is the right weight; a queue entry you have to go find is not.

### A3. `max_parallel_jobs`

With A0.1 fixed this setting works for the first time. Worth actually testing at
2 before trusting it — the AI providers rate-limit, and two concurrent Sonnet
calls on one lecture may just trade a queue for 429s.

---

## Workstream B — images from the source itself

### B0. The gap

`tools/images.py` is **web-only**: Wikipedia, Open-i, DermNet, Radiopaedia, Bing
(`search_images:306`). Every image entry point funnels through it — the
suggestion strip (`card_creator.py:3544`) and both picker buttons (`:1940`,
`:3731`). Nothing reads the PDF or PPTX you just uploaded.

Two near-misses that make the gap look smaller than it is:

- `pdf_render.render_pdf_to_images` gives you the **whole page as a screenshot**,
  attached per-card via `lecture_match` / `slide_mode`. That's the entire slide —
  title, bullets, footer — not the figure on it.
- `_extract_pptx_text` (`:1203`) opens the pptx zip and reads only
  `ppt/slides/*.xml`. Every embedded figure in `ppt/media/` is discarded.

So for a lecture whose whole value is one diagram, the add-on will either paste
the entire slide or go hunting on Wikipedia for a worse version of a picture it
is already holding.

### B1. Design — a local image source

New module `tools/source_images.py`, exposing one function:

```python
def extract(path: str) -> list[dict]:
    """[{path, page, caption, w, h}, ...] — every embedded raster in a
    PDF/PPTX, in document order. [] on any failure."""
```

**PPTX — easy.** It's a zip. `ppt/media/*` holds the rasters;
`ppt/slides/_rels/slideN.xml.rels` maps each slide to the media it uses, giving
the slide number (and therefore the surrounding text, already extracted by
`_extract_pptx_text`) for free. No dependency, ~80 lines.

**PDF — the real work.** PyMuPDF isn't bundled and QtPdf can't enumerate embedded
images, so this needs a small pure-Python pass over the PDF's XObject image
streams: walk the xref, find `/Subtype /Image`, pass `DCTDecode` through as JPEG
bytes and `FlateDecode` through zlib into a raw raster. That covers the large
majority of lecture slides. `JPXDecode` / `CCITTFaxDecode` get skipped rather
than half-decoded. ~200 lines, no dependency, and it fails to `[]` like every
other function in `pdf_render`.

**Junk filter.** Decks are full of university logos and bullet glyphs. Drop
anything under ~120px on either side, anything with an extreme aspect ratio, and
anything whose bytes repeat across more than ~30% of pages (that's the template).

### B2. Wiring — one new source, not a new subsystem

The point is that this becomes *a source among sources*, not a bolt-on panel.

1. `create_jobs` records already carry `attachment_paths`. Extract once during
   the new `preparing` phase (Workstream A), cache the results on the job.
2. `images.search_images` gains a `local=[...]` argument. Local results are
   scored against the card's `image_query` using the caption plus the text of the
   slide the figure came from, and are **ranked first** — they're free, offline,
   and provably from the material you're studying.
3. They therefore appear in the existing suggestion strip (`_on_candidates`,
   `:3553`) with a `📄` source badge. No new UI.
4. The picker dialog (`ImageSearchDialog`, `images.py:379`) gains a "From this
   source" tab: a grid of every figure in the deck, for when you want to browse
   rather than be suggested to.

Config: `images.sources.source_figures`, default **on** — unlike the web
backends it costs nothing and can't return something off-topic.

### B3. Sequencing

B depends on A only for the `preparing` phase (that's where extraction belongs).
If A slips, B can extract synchronously behind the existing `loading()` for a
first cut — it's much cheaper than rasterising — and move later.

---

## Already done (not part of the sign-off)

- `uses_collection=False` on all 8 `run_in_background` call sites that were
  missing it: `create_jobs.py:284`, `qt_utils.py:113`/`:364`, `browse.py:1715`,
  `welcome.py` ×3, `settings.py:1062`. Anki's `_collection_executor` is a
  single-worker pool that its periodic backup queues behind.
- Image display caps. `img_tag()` (`card_creator.py`) is now the only place the
  add-on writes an `<img>`, and it stamps
  `style="max-width:…; max-height:…"` — 400×560 for card figures, 700×560 for
  whole slides (a slide capped at 400px is unreadable). Configurable under
  `card_creator.images`.
- Transparency flattening. QtPdf renders a page with a **transparent**
  background — alpha 0 everywhere the PDF paints nothing. Saved to PNG that
  survived into media, so in night mode you were reading black slide text through
  a dark note background. `pdf_render._flatten_onto_white` composites every
  rendered page onto white, and `_normalize_image_file` flattens any alpha image
  (web diagrams have the same problem). Toggle:
  `card_creator.images.flatten_transparency`.
- Source citation off by default. `_enrich_extra` stamped
  `Source: <filename>` onto every card's Extra — which for a journal PDF is
  `Source: PIIS1443950618317785.pdf`. The tags already record provenance. Re-enable
  with `card_creator.cite_source`.
