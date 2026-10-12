# Mass AI Create — generate cards for a whole selection of knowledge gaps.
#
# The generation-side twin of bulk_search.py. Same three-beat shape, because the
# problem is the same one: a batch of gaps whose breadth varies wildly, which
# must be SIZED before it runs and REVIEWED after.
#
#   page 0  plan table   — one row per gap: what the AI read it as, how broad,
#                          how many cards, which tag. Editable before anything runs.
#   page 1  running      — per-gap status while the jobs generate.
#   page 2  batch review — every generated card, grouped by gap, approved and
#                          committed in ONE undoable step.
#
# WHY THE JOBS AREN'T OWNED BY THIS DIALOG.
# The run *feels* dialog-owned — its own progress bar, its own Stop, and closing
# mid-run offers to keep going — but the work itself is ordinary create_jobs
# records on disk. That runner already has restart recovery, per-job cancel and
# retry, and the uses_collection=False discipline that keeps a long generation
# from queueing behind Anki's backup. So the dialog POLLS a batch_id rather than
# holding threads: a crash or a quit mid-batch loses nothing, and reopening a
# batch afterwards is the same code path as watching one live.

from __future__ import annotations


from aqt import mw

from ...core import anki_utils


NAME = "Mass AI Create"

# How often the running page re-reads the job store. Fast enough that a job
# flipping to "ready" feels immediate, slow enough that a 12-row rebuild every
# tick costs nothing.
POLL_MS = 750

HELP_HTML = """
<h3>Mass AI Create</h3>
<p>Generates cards for a whole selection of knowledge gaps in one run.</p>
<h4>1. Plan</h4>
<p><b>Read gaps with AI</b> sends every ticked gap to one call that decides, for
each, how broad it is and how many cards it's worth — then decomposes it into the
specific points the cards must cover. Everything it decides is editable: change
the <b>Breadth</b> dropdown or type over the <b>Cards</b> number.</p>
<p>Breadth moves two things at once: how much ground the cards cover, and how
deep each one cuts. <i>Wide</i> and <i>Deep</i> cover the same ground —
<i>Deep</i> goes past the headlines into mechanism.</p>
<p>The card number is a <b>ceiling, not a quota</b>. A gap with little in it
comes back with fewer cards, and that's correct.</p>
<h4>2. Generate</h4>
<p>One AI call per gap, two at a time (raise that in Settings &rarr; AI Create).
You can close this window — the run keeps going, and a crash or a restart won't
lose it.</p>
<h4>3. Review</h4>
<p>Every card, grouped by the gap it came from. Cards start approved; untick the
ones you don't want. <b>Add approved</b> writes them all in a single step, so one
Ctrl+Z takes the whole batch back out.</p>
<p><i>Skip what a deck or tag covers</i> searches a scope you name — a tag your
AI Search results were filed under, say — and drops any point already covered,
so Create only fills what's actually missing.</p>
"""


# ── helpers ──────────────────────────────────────────────────────────────────

def _deck_names() -> list[str]:
    try:
        return sorted(d.name for d in mw.col.decks.all_names_and_ids())
    except Exception:
        return []


def _notetype_profiles(cfg: dict) -> list[dict]:
    profs = [p for p in (cfg.get("notetypes") or []) if isinstance(p, dict)]
    return [p for p in profs if (p.get("name") or "").strip()]


def _norm_front(card: dict) -> str:
    """A card's front, flattened for near-duplicate comparison within a batch."""
    try:
        txt = anki_utils.strip_cloze(anki_utils.strip_html(card.get("front", "")))
    except Exception:
        txt = str(card.get("front", ""))
    return " ".join(txt.lower().split())[:60]


# ── the dialog ───────────────────────────────────────────────────────────────

def commit_ctx_for(job: dict, profile: dict, tags: list):
    """A job's CommitCtx — the batch's profile, but this gap's own tags and KG
    content, so an MQ gap still composes its Missed Questions field correctly
    inside a mixed batch.

    Module-level because two surfaces now build cards from the same job records:
    this dialog's review blocks, and the unified pass, which renders the written
    cards straight into its results tree and so has no block to ask."""
    from .. import card_creator as cc
    import os as _os
    # The job's OWN mode and source label, not a hardcoded "topic". They were
    # hardcoded because every job this saw came from mass create, where both are
    # always topic/None — but AI Create's jobs now commit through here too, and
    # a source-mode job committed as topic loses its citation (enrich_extra
    # keys off exactly these two).
    try:
        media_dir = mw.col.media.dir()
    except Exception:
        media_dir = ""
    panel_paths = [_os.path.join(media_dir, fn) if media_dir else fn
                   for fn in (job.get("panel_image_filenames") or []) if fn]
    return cc.CommitCtx(
        profile=profile,
        tags=list(tags),
        mode=str(job.get("mode") or "topic"),
        source_label=job.get("source_label"),
        # Images the user attached in the Create panel. Empty for every mass
        # create job (it has no panel), which is why this used to be a literal
        # [] — an AI Create job carries them and must not lose them.
        panel_image_paths=panel_paths,
        kg_image_filenames=list(job.get("kg_image_filenames") or []),
        kg_type_for_image=(job.get("kg_type_for_image") or "").lower(),
        kg_stem_html=str(job.get("kg_stem_html") or ""),
        kg_notes=str(job.get("kg_notes") or ""),
        kg_concept=str(job.get("kg_concept") or ""),
        kg_explanation=str(job.get("kg_explanation") or ""),
        # The gap's reading list. Off the job where the queue carried it, off
        # the gap record otherwise — an older job predates the key.
        kg_resources=list(
            job.get("kg_resources")
            or (job.get("gap") if isinstance(job.get("gap"), dict) else {})
            .get("resources") or []),
        type_meta=job.get("type_meta") if isinstance(job.get("type_meta"), dict) else None,
        # From the GAP, not the job: the worker persists AI-written field values
        # back onto the gap record (create_jobs._apply_result_main_thread), so
        # that is where a freshly-written explanation actually lives.
        kg_field_values=cc._gap_field_values(
            job.get("gap") if isinstance(job.get("gap"), dict) else None),
    )


# ── one gap's cards, in the review page ──────────────────────────────────────

# ── entry point ──────────────────────────────────────────────────────────────

