# Extraction cache — the expensive, deterministic-enough first read.
#
# Extraction is one long AI reasoning call (~240s text-only, ~$0.15 in tokens)
# over the WHOLE lecture. When you iterate on the downstream steps (matching,
# relevance, allocation) you re-run the SAME file with the SAME extraction
# settings over and over — re-paying that call every time for an identical
# result. This caches the learning points on disk, keyed on the file's bytes
# plus the settings that actually change the extraction, so repeat runs skip
# straight to matching for free.
#
# The AI reply isn't bit-for-bit deterministic, so a cache hit is "a plausible
# prior extraction of this exact file+settings", not "the canonical answer".
# That's the right trade for a tuning loop: you're not tuning extraction, you're
# tuning what happens after it. Bump _CACHE_VERSION (or delete the folder) to
# force a fresh read; the panel also exposes a "re-extract" escape hatch.

from __future__ import annotations

import hashlib
import json
import os
from dataclasses import asdict

from ...core import log
from .extractor import LearningPoint

# Bump when LearningPoint's shape or the extraction prompt changes materially,
# so stale entries can't resurface a structure the rest of the pipeline no
# longer understands.
#   v2 (2026-07-23): added the "High-yield only" low-yield filter to
#   EXTRACT_SYSTEM. Old v1 entries were extracted without it.
#   v3 (2026-07-23): term rules hardened to anchor on the FULL name, not the
#   abbreviation (verified miss: 'PD rigidity' matched 24 cards, 'Parkinson'
#   343). Old v2 terms were generated abbreviation-first and under-recall.
#   v4 (2026-07-25): "enumerate the members of a named class" rule added — an
#   enumerable clinical set (stroke territories, cranial-nerve palsies, shock
#   types) now decomposes into a point per member instead of one coarse point
#   that silently covered only its loudest member. Old v3 extractions are
#   under-decomposed and would resurface those gaps.
#   v5 (2026-07-26): two prompt changes. (a) "one idea per point — split a
#   bundled aspect list": a bullet naming a structure plus several distinct
#   aspects (cortex: lobes, layers, cytoarchitecture) now splits into a point
#   per aspect. (b) source-fidelity gate: by default the model must NOT backfill
#   canonical facts the material omits (fixed the invented regional-V/Q point);
#   the new `enrich` flag (in the key) opts back into supplementation. Old v4
#   extractions are under-split and may contain enrichment the user didn't ask
#   for.
#   v6 (2026-07-31): LearningPoint gained `kg_id` (KG mode — see
#   tools/kg/triage.py). Old v5 entries still deserialise (it defaults to ""),
#   but the bump keeps the on-disk shape and the prompt set in lockstep.
#   v7 (2026-08-01): companion text (the user's own notes / a transcript). The
#   extract prompt gained a @@COMPANION_RULE@@ section and LearningPoint gained
#   `src` + `note_refs`. The companion text itself is in the key below, so
#   editing your notes re-extracts; the bump covers the prompt change for runs
#   with no notes at all.
_CACHE_VERSION = 7


def _cache_dir() -> str | None:
    try:
        root = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
        d = os.path.join(root, "user_files", "lecture_cache")
        os.makedirs(d, exist_ok=True)
        return d
    except Exception:
        return None


def _file_digest(path: str) -> str | None:
    """SHA1 of the file's bytes — edit the lecture and the key changes."""
    try:
        h = hashlib.sha1()
        with open(path, "rb") as f:
            for chunk in iter(lambda: f.read(1 << 20), b""):
                h.update(chunk)
        return h.hexdigest()
    except Exception:
        return None


def companion_digest(companion) -> str:
    """A stable fingerprint of the user's notes, for the cache keys.

    Empty string when there are none, which keeps the key byte-identical to a
    run without the feature — so nobody who never touches it pays a cache miss
    for its existence.
    """
    chunks = getattr(companion, "chunks", None) or []
    if not chunks:
        return ""
    h = hashlib.sha1()
    for c in chunks:
        h.update((getattr(c, "text", "") or "").encode("utf-8", "replace"))
        h.update(b"\x00")
    return h.hexdigest()[:16]


def cache_key(path: str, *, target_points: int, use_vision: bool,
              model: str | None, high_yield: bool = False,
              enrich: bool = False, companion: str = "") -> str | None:
    """Identity of an extraction: the file's bytes + every setting that changes
    what comes back. Miss on any of them and we re-extract, which is correct —
    a different point target, turning vision on, the high-yield filter, the
    enrich (backfill-beyond-the-lecture) toggle, or a change to the notes you
    handed over is a genuinely different read.

    `companion` is companion_digest(...) — "" when no notes were supplied, so the
    key is unchanged for anyone not using the feature.
    """
    digest = _file_digest(path)
    if not digest:
        return None
    parts = (f"{digest}|{target_points}|{int(use_vision)}|{int(high_yield)}"
             f"|{int(enrich)}|{model or ''}|{_CACHE_VERSION}")
    if companion:
        parts += f"|C{companion}"
    return hashlib.sha1(parts.encode("utf-8")).hexdigest()


def cache_key_text(text: str, *, model: str | None,
                   enrich: bool = False, companion: str = "") -> str | None:
    """Identity of an LO-mode extraction: the pasted objective text + model +
    enrich toggle + any companion notes. LO extraction is cheap, but caching it
    still lets a matching/relevance tuning loop re-run the same objectives for
    free. No point target or vision flag here — LO mode uses neither."""
    if not (text or "").strip():
        return None
    norm = "\n".join(line.strip() for line in text.strip().splitlines() if line.strip())
    parts = f"LO|{norm}|{int(enrich)}|{model or ''}|{_CACHE_VERSION}"
    if companion:
        parts += f"|C{companion}"
    return hashlib.sha1(parts.encode("utf-8")).hexdigest()


def load(key: str | None) -> list[LearningPoint] | None:
    """Return cached points for this key, or None on any miss/corruption —
    the caller then extracts normally. Never raises."""
    if not key:
        return None
    d = _cache_dir()
    if not d:
        return None
    fp = os.path.join(d, key + ".json")
    try:
        with open(fp, "r", encoding="utf-8") as f:
            raw = json.load(f)
        pts = [LearningPoint(**item) for item in raw]
        log.info(f"lecture: extraction cache HIT ({len(pts)} points) — skipping the AI read")
        return pts
    except FileNotFoundError:
        return None
    except Exception as e:
        log.warn(f"lecture: extraction cache unreadable, re-extracting: {e}")
        return None


def store(key: str | None, points: list[LearningPoint]) -> None:
    """Persist points under this key. Best-effort; a cache write must never
    sink a run that already paid for the extraction."""
    if not key or not points:
        return
    d = _cache_dir()
    if not d:
        return
    fp = os.path.join(d, key + ".json")
    try:
        raw = [asdict(p) for p in points]
        tmp = fp + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(raw, f, ensure_ascii=False)
        os.replace(tmp, fp)
        log.info(f"lecture: cached extraction ({len(points)} points)")
    except Exception as e:
        log.warn(f"lecture: could not cache extraction: {e}")
