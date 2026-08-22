# Lecture ingest — a source file into an ordered list of segments.
#
# "Lecture" is deliberately loose: a PowerPoint deck, a PDF export of one, a
# Word handout, a PBL block doc, or a plain-text set of notes. Each format has
# its own natural unit (slide / page / section), so a Segment carries its unit
# label rather than assuming slides — the rest of the pipeline only ever sees
# numbered segments with text.
#
# Text-first: every supported format can usually be read with no AI at all.
# Only segments that come back empty (a scanned page, or a slide that's one big
# diagram) need the vision fallback, and the caller decides that by looking at
# `Lecture.vision_segments` rather than paying for vision on the whole file.
#
# No third-party dependencies: PDFs go through the QtPdf module Anki already
# bundles (via ..pdf_render); .pptx and .docx are both just zips of XML.

from __future__ import annotations

import os
import re
import shutil
import subprocess
import xml.etree.ElementTree as ET
import zipfile
from dataclasses import dataclass, field

from ...core import log
from .. import pdf_render


# Segments shorter than this are treated as having no usable text — a
# title-only slide ("Cardiac Failure") or a stray page number tells the
# extractor nothing it can build a learning point from, so it's routed to
# vision like a genuinely blank one.
MIN_USABLE_CHARS = 25

# Distinct quantised colours above which a rendered segment is treated as
# carrying imagery (see detect_imagery). Measured on a real 44-page deck:
# text-only pages sit at exactly 16, genuine diagrams at 82–182. 80 sits in the
# empty middle of that gap, so it isn't a knife-edge.
IMAGERY_MIN_COLOURS = 80
IMAGERY_SAMPLE_EDGE = 220   # target samples along the short edge

# Mirrors pdf_render.DEFAULT_MAX_PAGES so a 300-page handout can't hang Anki.
MAX_SEGMENTS = pdf_render.DEFAULT_MAX_PAGES

# A Word doc with no headings gets chunked into fixed runs of paragraphs so it
# still batches sensibly instead of becoming one giant segment.
DOCX_PARAS_PER_SECTION = 12

_A_NS = "{http://schemas.openxmlformats.org/drawingml/2006/main}"          # pptx text
_P_NS = "{http://schemas.openxmlformats.org/presentationml/2006/main}"     # pptx shapes
_W_NS = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"   # docx
_PKG_REL_NS = "{http://schemas.openxmlformats.org/package/2006/relationships}"

# Placeholder shapes that carry chrome rather than content. The notes master
# stamps the slide number into every notes slide, so without this filter the
# "presenter notes" for slide 7 come out as the string "7".
_CHROME_PLACEHOLDERS = {"sldNum", "dt", "ftr"}

# suffix -> unit noun used in the prompt and shown to the user
SUPPORTED_SUFFIXES = {
    ".pptx": "Slide",
    ".pdf": "Page",
    ".docx": "Section",
    ".txt": "Section",
    ".md": "Section",
    # A video's transcript IS its material — there are no slides to read. The
    # unit is "Part" because there's nothing to number but the running order.
    ".rtf": "Section",
    ".vtt": "Part",
    ".srt": "Part",
}


@dataclass
class Segment:
    number: int          # 1-based ordinal within the file
    unit: str = "Slide"  # "Slide" | "Page" | "Section"
    text: str = ""       # extracted text; "" when image-only
    notes: str = ""      # presenter notes (.pptx only)
    title: str = ""      # heading this segment sits under (.docx only)

    @property
    def label(self) -> str:
        base = f"{self.unit} {self.number}"
        return f"{base}: {self.title}" if self.title else base

    @property
    def has_text(self) -> bool:
        return len((self.text or "").strip()) >= MIN_USABLE_CHARS

    def as_prompt_block(self) -> str:
        """The form the extractor sends to the AI. The number in the header is
        what the model cites back, so it must always be present."""
        out = [f"## {self.label}"]
        if self.text.strip():
            out.append(self.text.strip())
        if self.notes.strip():
            out.append(f"[presenter notes] {self.notes.strip()}")
        if not self.text.strip() and not self.notes.strip():
            out.append(f"[no extractable text — image-only {self.unit.lower()}]")
        return "\n".join(out)


@dataclass
class Lecture:
    path: str
    title: str
    unit: str = "Slide"
    segments: list[Segment] = field(default_factory=list)
    truncated: bool = False   # source had more segments than MAX_SEGMENTS

    @property
    def rasterisable(self) -> bool:
        """Whether image-only segments can be recovered by the vision pass.
        Only PDFs can be rendered — .pptx/.docx would need a real renderer, so
        their image content is simply unavailable and the panel says so."""
        return os.path.splitext(self.path)[1].lower() == ".pdf"

    @property
    def vision_segments(self) -> list[int]:
        """Numbers of segments with no usable text. If this is most of the file
        it's a scan, and the caller should say so rather than silently running
        an expensive vision pass over everything."""
        return [s.number for s in self.segments if not s.has_text]

    @property
    def is_mostly_images(self) -> bool:
        if not self.segments:
            return False
        return len(self.vision_segments) > len(self.segments) // 2


class UnsupportedLecture(Exception):
    """Raised for a file type we can't read, or a file we can read but that
    yielded nothing at all."""


def _natural_key(name: str):
    """Sort 'slide2.xml' before 'slide10.xml'."""
    return [int(c) if c.isdigit() else c for c in re.split(r"(\d+)", name)]


def _title_from_path(path: str) -> str:
    stem = os.path.splitext(os.path.basename(path))[0]
    # Lecture files are habitually named "2026-03-04 Cardiac Failure LEC3" —
    # strip leading dates so the default tag leaf is clean.
    stem = re.sub(r"^\d{2,4}[-_ ]\d{1,2}[-_ ]\d{1,4}[-_ ]*", "", stem)
    stem = re.sub(r"[-_]+", " ", stem)
    # Collapse the runs the substitution above creates — "Block 1 - GI" would
    # otherwise come out as "Block 1   GI".
    stem = re.sub(r"\s+", " ", stem)
    return stem.strip() or "Lecture"


# ── pptx ─────────────────────────────────────────────────────────────────────

def _para_text(node, ns: str = _A_NS) -> list[str]:
    """Every non-empty paragraph under `node`, in document order."""
    out: list[str] = []
    for para in node.iter(ns + "p"):
        runs = [t.text or "" for t in para.iter(ns + "t")]
        line = "".join(runs).strip()
        if line:
            out.append(line)
    return out


def _placeholder_type(shape) -> str:
    """The `type` of a shape's placeholder ("title", "body", "sldNum", …), or
    "" when the shape isn't a placeholder at all."""
    for ph in shape.iter(_P_NS + "ph"):
        return ph.get("type", "body")
    return ""


def _content_text(root) -> list[str]:
    """Paragraph text from a slide/notes tree, skipping chrome placeholders.

    Walks shapes rather than the whole tree so a slide-number or footer
    placeholder can be excluded — `root.iter(a:t)` would happily hand back "7"
    as if it were content. Table text lives outside p:sp and is collected
    separately.
    """
    out: list[str] = []
    seen_shape = False
    for shape in root.iter(_P_NS + "sp"):
        seen_shape = True
        if _placeholder_type(shape) in _CHROME_PLACEHOLDERS:
            continue
        out.extend(_para_text(shape))

    for frame in root.iter(_P_NS + "graphicFrame"):
        out.extend(_para_text(frame))

    if not out and not seen_shape:
        # Unfamiliar shape layout — better a noisy read than an empty one.
        out.extend(_para_text(root))
    return out


def _notes_by_slide(zf: zipfile.ZipFile, slide_names: list[str]) -> dict[int, str]:
    """Map 1-based slide index → presenter notes.

    notesSlideN.xml does NOT belong to slideN.xml: the link is declared in
    ppt/slides/_rels/slideN.xml.rels, which points at the notes slide. Pairing
    by number silently attaches the wrong notes to the wrong slide whenever a
    deck has notes on only some slides — which is the normal case.
    """
    notes: dict[int, str] = {}
    for i, slide_name in enumerate(slide_names, 1):
        rels_path = f"ppt/slides/_rels/{os.path.basename(slide_name)}.rels"
        try:
            with zf.open(rels_path) as fh:
                rels = ET.parse(fh).getroot()
        except (KeyError, ET.ParseError):
            continue

        target = ""
        for rel in rels.iter(_PKG_REL_NS + "Relationship"):
            if rel.get("Type", "").endswith("/notesSlide"):
                target = rel.get("Target", "")
                break
        if not target:
            continue

        # Targets are relative to ppt/slides/ — "../notesSlides/notesSlide3.xml".
        notes_path = os.path.normpath(os.path.join("ppt/slides", target)).replace(os.sep, "/")
        try:
            with zf.open(notes_path) as fh:
                root = ET.parse(fh).getroot()
        except (KeyError, ET.ParseError):
            continue

        text = "\n".join(_content_text(root)).strip()
        if text:
            notes[i] = text
    return notes


def _load_pptx(path: str) -> list[Segment]:
    """A .pptx is a zip of XML: walk slides in natural order, then attach each
    slide's notes via its relationships file."""
    segments: list[Segment] = []
    with zipfile.ZipFile(path) as zf:
        slide_names = sorted(
            (n for n in zf.namelist()
             if n.startswith("ppt/slides/slide") and n.endswith(".xml")),
            key=_natural_key,
        )
        notes = _notes_by_slide(zf, slide_names)

        for i, name in enumerate(slide_names, 1):
            try:
                with zf.open(name) as fh:
                    root = ET.parse(fh).getroot()
            except ET.ParseError:
                # One corrupt slide shouldn't sink the deck — record it as
                # image-only so the vision pass can still pick it up.
                log.warn(f"lecture: pptx slide {i} unparseable, treating as image-only")
                segments.append(Segment(number=i, unit="Slide", notes=notes.get(i, "")))
                continue
            segments.append(Segment(
                number=i,
                unit="Slide",
                text="\n".join(_content_text(root)),
                notes=notes.get(i, ""),
            ))
    return segments


# ── docx ─────────────────────────────────────────────────────────────────────

def _docx_style(para) -> str:
    for pr in para.iter(_W_NS + "pPr"):
        for style in pr.iter(_W_NS + "pStyle"):
            return (style.get(_W_NS + "val") or "").lower()
    return ""


def _docx_para_text(para) -> str:
    return "".join(t.text or "" for t in para.iter(_W_NS + "t")).strip()


def _docx_looks_bold(para) -> bool:
    """Whether every text-bearing run in the paragraph is bold.

    Plenty of real documents — Curtin's PBL block docs among them — use no
    heading styles at all and mark sections with direct formatting instead
    (bold, larger point size). Style-based detection alone reads those as one
    undifferentiated slab.
    """
    runs = [r for r in para.iter(_W_NS + "r") if r.find(_W_NS + "t") is not None]
    if not runs:
        return False
    for run in runs:
        pr = run.find(_W_NS + "rPr")
        if pr is None or pr.find(_W_NS + "b") is None:
            return False
    return True


# A heading is short. Anything longer is a bolded sentence, not a section title.
_DOCX_HEADING_MAX_CHARS = 120
# If most paragraphs look like headings, the document is simply bold throughout
# and the heuristic is meaningless — fall back to fixed chunking.
_DOCX_HEADING_MAX_SHARE = 0.4


def _load_docx(path: str) -> list[Segment]:
    """A .docx is a zip whose word/document.xml holds a flat run of paragraphs.

    Split on headings so each segment is a topic rather than an arbitrary slab
    — a PBL block doc's "Learning Objectives" and "Case" become separate
    segments. A doc with no headings falls back to fixed-size paragraph runs.
    """
    with zipfile.ZipFile(path) as zf:
        try:
            with zf.open("word/document.xml") as fh:
                root = ET.parse(fh).getroot()
        except KeyError:
            raise UnsupportedLecture("That .docx has no document body.")
        except ET.ParseError:
            raise UnsupportedLecture("That .docx looks corrupt — the body wouldn't parse.")

    body = root.find(_W_NS + "body")
    if body is None:
        raise UnsupportedLecture("That .docx has no document body.")

    # Tables are separate elements; walk the body in document order so a table
    # stays with the heading above it.
    blocks: list[tuple[str, str]] = []   # (kind, text) where kind is "heading"|"para"
    for child in body:
        tag = child.tag
        if tag == _W_NS + "p":
            text = _docx_para_text(child)
            if not text:
                continue
            style = _docx_style(child)
            if "heading" in style or style == "title":
                kind = "heading"
            elif not style and _docx_looks_bold(child) and len(text) <= _DOCX_HEADING_MAX_CHARS:
                kind = "maybe_heading"
            else:
                kind = "para"
            blocks.append((kind, text))
        elif tag == _W_NS + "tbl":
            rows: list[str] = []
            for row in child.iter(_W_NS + "tr"):
                cells = [
                    " ".join(_docx_para_text(p) for p in cell.iter(_W_NS + "p")).strip()
                    for cell in row.iter(_W_NS + "tc")
                ]
                cells = [c for c in cells if c]
                if cells:
                    rows.append(" | ".join(cells))
            if rows:
                blocks.append(("para", "\n".join(rows)))

    if not blocks:
        return []

    # Real heading styles win outright. Failing those, promote the bold-and-short
    # paragraphs — but only if they're a minority, since a uniformly bold
    # document would otherwise become all headings and no content.
    if any(k == "heading" for k, _ in blocks):
        blocks = [(("para" if k == "maybe_heading" else k), t) for k, t in blocks]
    else:
        maybes = sum(1 for k, _ in blocks if k == "maybe_heading")
        promote = 0 < maybes <= len(blocks) * _DOCX_HEADING_MAX_SHARE
        blocks = [
            (("heading" if (k == "maybe_heading" and promote) else
              "para" if k == "maybe_heading" else k), t)
            for k, t in blocks
        ]

    has_headings = any(k == "heading" for k, _ in blocks)
    segments: list[Segment] = []

    if has_headings:
        cur_title = ""
        cur_body: list[str] = []

        def _flush():
            if cur_title or cur_body:
                segments.append(Segment(
                    number=len(segments) + 1,
                    unit="Section",
                    title=cur_title,
                    text="\n".join(cur_body).strip(),
                ))

        for kind, text in blocks:
            if kind == "heading":
                _flush()
                cur_title, cur_body = text, []
            else:
                cur_body.append(text)
        _flush()
        # Drop a leading empty section from a doc that opens with body text
        # before its first heading only if it truly carried nothing.
        segments = [s for s in segments if s.text.strip() or s.title.strip()]
        for i, s in enumerate(segments, 1):
            s.number = i
    else:
        paras = [t for _, t in blocks]
        for i in range(0, len(paras), DOCX_PARAS_PER_SECTION):
            segments.append(Segment(
                number=len(segments) + 1,
                unit="Section",
                text="\n".join(paras[i:i + DOCX_PARAS_PER_SECTION]).strip(),
            ))
    return segments


# ── rich text ────────────────────────────────────────────────────────────────

# Destination groups whose contents are machinery, not prose. Anything inside
# one of these is skipped whole, along with any `{\*\...}` group.
_RTF_SKIP_DESTINATIONS = {
    "fonttbl", "colortbl", "stylesheet", "expandedcolortbl", "listtable",
    "listoverridetable", "info", "pict", "object", "themedata", "datastore",
    "latentstyles", "generator", "xmlnstbl", "filetbl", "rsidtbl",
}
_RTF_TOKEN_RE = re.compile(r"\\'([0-9a-fA-F]{2})|\\([a-zA-Z]+)(-?\d+)?[ ]?|\\(.)|([{}])|([^\\{}]+)")


def _rtf_to_text(raw: str) -> str:
    """Plain text out of an RTF, well enough for a transcript.

    A real tokeniser rather than a sweep of regex substitutions, because the
    regex version lost words: an auto-transcript ends each line with a bare
    backslash, and `\\` immediately followed by `called` reads as the control
    word `\\called` — so every word that happened to start a line disappeared.
    Silent, and invisible unless you diff the word count. Group depth is tracked
    properly here so font and colour tables are skipped whole instead of leaking
    'Times-Roman;Times-Bold;' into the first segment.
    """
    out: list[str] = []
    depth = 0
    skip_until: int | None = None      # depth at which the skipped group opened

    for m in _RTF_TOKEN_RE.finditer(raw or ""):
        hexb, word, _arg, esc, brace, text = m.groups()
        if brace:
            if brace == "{":
                depth += 1
            else:
                depth -= 1
                if skip_until is not None and depth < skip_until:
                    skip_until = None
            continue
        if skip_until is not None:
            continue
        if word:
            if word in _RTF_SKIP_DESTINATIONS:
                skip_until = depth
            elif word in ("par", "line", "pard"):
                out.append("\n")
            elif word in ("tab",):
                out.append(" ")
            continue
        if esc:
            if esc == "*":
                # `{\*\something}` is RTF for "ignorable destination" — skip the
                # whole group, whatever it turns out to be.
                skip_until = depth
                continue
            # A backslash before a newline is RTF's soft line break; before {, }
            # or \ it is a literal character.
            out.append("\n" if esc in "\r\n" else esc)
            continue
        if hexb:
            try:
                out.append(bytes([int(hexb, 16)]).decode("cp1252", "replace"))
            except ValueError:
                pass
            continue
        if text:
            out.append(text)

    lines = [re.sub(r"[ \t]+", " ", ln).strip() for ln in "".join(out).splitlines()]
    return "\n".join(ln for ln in lines if ln)


def _load_captions(path: str) -> list[Segment]:
    """A .vtt/.srt as a PRIMARY source. Same cue stripping the supplementary
    slot uses, then split into segments rather than placeable chunks."""
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as fh:
            raw = fh.read()
    except OSError as e:
        raise UnsupportedLecture(f"Couldn't read that file: {e}")
    body = _regroup_sentences(_strip_cues(raw), target_chars=TRANSCRIPT_SEGMENT_CHARS)
    return [Segment(number=i, unit="Part", text=t.strip())
            for i, t in enumerate(re.split(r"\n\s*\n", body), 1) if t.strip()]


def _load_rtf(path: str) -> list[Segment]:
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as fh:
            raw = fh.read()
    except OSError as e:
        raise UnsupportedLecture(f"Couldn't read that file: {e}")
    return _segments_from_text(_rtf_to_text(raw))


# ── plain text ───────────────────────────────────────────────────────────────

def _load_text(path: str) -> list[Segment]:
    """Markdown/plain notes: split on markdown headings when present, else on
    blank-line paragraph runs."""
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as fh:
            raw = fh.read()
    except OSError as e:
        raise UnsupportedLecture(f"Couldn't read that file: {e}")
    return _segments_from_text(raw)


def _segments_from_text(raw: str) -> list[Segment]:
    if not raw.strip():
        return []

    # A transcript has no headings and no paragraphs — a video is one unbroken
    # run of talking. Left to the paragraph rule below, a 17k-character
    # transcript becomes two or three enormous segments, which extraction then
    # has to summarise rather than read. Reflowed, it behaves like a deck of
    # slides: many small numbered units, which is what the whole pipeline
    # downstream of here expects.
    if _looks_like_transcript(raw):
        body = _regroup_sentences(_strip_speakers(raw),
                                  target_chars=TRANSCRIPT_SEGMENT_CHARS)
        return [Segment(number=i, unit="Part", text=t.strip())
                for i, t in enumerate(re.split(r"\n\s*\n", body), 1) if t.strip()]

    lines = raw.splitlines()
    if any(re.match(r"^#{1,6}\s+\S", ln) for ln in lines):
        segments: list[Segment] = []
        cur_title, cur_body = "", []

        def _flush():
            if cur_title or [b for b in cur_body if b.strip()]:
                segments.append(Segment(
                    number=len(segments) + 1, unit="Section",
                    title=cur_title, text="\n".join(cur_body).strip(),
                ))

        for ln in lines:
            m = re.match(r"^#{1,6}\s+(.*)$", ln)
            if m:
                _flush()
                cur_title, cur_body = m.group(1).strip(), []
            else:
                cur_body.append(ln)
        _flush()
        return segments

    chunks = [c.strip() for c in re.split(r"\n\s*\n", raw) if c.strip()]
    out: list[Segment] = []
    for i in range(0, len(chunks), 3):
        out.append(Segment(number=len(out) + 1, unit="Section",
                           text="\n\n".join(chunks[i:i + 3])))
    return out


# ── pdf ──────────────────────────────────────────────────────────────────────

def _load_pdf(path: str) -> list[Segment]:
    pages = pdf_render.extract_pdf_text_per_page(path)
    if not pages:
        # [] means the load itself failed, which is different from "every page
        # is an image" — that would be a list of empty strings.
        raise UnsupportedLecture(
            "Couldn't read that PDF. It may be corrupt or password-protected."
        )
    return [Segment(number=i, unit="Page", text=t or "") for i, t in enumerate(pages, 1)]


# ── entry point ──────────────────────────────────────────────────────────────

_LOADERS = {
    ".pptx": _load_pptx,
    ".pdf": _load_pdf,
    ".docx": _load_docx,
    ".txt": _load_text,
    ".md": _load_text,
    ".rtf": _load_rtf,
    # .vtt/.srt are transcripts by format. They're accepted as a PRIMARY source
    # too: an assigned video has no slides, so its transcript IS the material.
    ".vtt": _load_captions,
    ".srt": _load_captions,
}


def load(path: str) -> Lecture:
    """Read a lecture file into segments. Raises UnsupportedLecture for a file
    type we don't handle or a file that yielded nothing at all."""
    suffix = os.path.splitext(path)[1].lower()
    if suffix not in SUPPORTED_SUFFIXES:
        supported = ", ".join(sorted(SUPPORTED_SUFFIXES))
        raise UnsupportedLecture(
            f"{suffix or 'That file'} isn't supported — use one of: {supported}."
        )
    if not os.path.exists(path):
        raise UnsupportedLecture("That file no longer exists.")

    try:
        segments = _LOADERS[suffix](path)
    except UnsupportedLecture:
        raise
    except zipfile.BadZipFile:
        raise UnsupportedLecture(f"That {suffix} looks corrupt — Anki couldn't unzip it.")
    except Exception as e:
        log.error(f"lecture: ingest failed for {path}: {e}")
        raise UnsupportedLecture(f"Couldn't read that lecture: {e}")

    if not segments:
        raise UnsupportedLecture("That file has no readable content in it.")

    truncated = len(segments) > MAX_SEGMENTS
    if truncated:
        log.warn(f"lecture: {len(segments)} segments, capping at {MAX_SEGMENTS}")
        segments = segments[:MAX_SEGMENTS]

    # The suffix gives the usual unit noun, but a loader may have decided
    # otherwise — a .txt of transcript comes back as Parts, not Sections. Follow
    # the segments, so the lecture and its parts never disagree in the UI.
    unit = segments[0].unit if segments else SUPPORTED_SUFFIXES[suffix]

    return Lecture(
        path=path,
        title=_title_from_path(path),
        unit=unit,
        segments=segments,
        truncated=truncated,
    )


def detect_imagery(path: str, numbers: list[int] | None = None) -> set[int]:
    """Segment numbers whose rendered page carries pictures, diagrams or tables.

    Why this exists: `has_text` is not enough. A slide can have a full text layer
    and still teach nothing through it — a drug-to-class matching table extracts
    as an unordered bag of words, because the pairing lives in the layout. Those
    segments must reach vision even though they look textually complete.

    The measure is distinct quantised colours over a subsampled render. Body text
    is near-monochrome (a real deck's text-only slides sit at ~16 colours);
    anything drawn or photographed pushes it far higher.

    This CANNOT judge yield, and must never be asked to — the most colourful
    pages in a real lecture were its acknowledgement and closing slides, not its
    pharmacology. It answers "is there something to look at", nothing more; what
    is worth keeping is decided downstream by the model and by which segments the
    learning points actually cite.

    PDF-only, like render_segment_images(). Returns an empty set on any failure:
    imagery detection is an enhancement, and a lecture must still process without it.
    """
    if os.path.splitext(path)[1].lower() != ".pdf":
        return set()
    try:
        from aqt.qt import QImage
    except Exception:
        return set()
    try:
        paths = pdf_render.render_pdf_to_images(path)
    except Exception as e:
        log.warn(f"lecture: imagery detection skipped for {path}: {e}")
        return set()

    wanted = set(numbers) if numbers else set(range(1, len(paths) + 1))
    found: set[int] = set()
    for n in sorted(wanted):
        idx = n - 1
        if not (0 <= idx < len(paths)):
            continue
        try:
            if _distinct_colours(QImage(paths[idx])) >= IMAGERY_MIN_COLOURS:
                found.add(n)
        except Exception:
            continue
    return found


def _distinct_colours(img) -> int:
    """Count of distinct 12-bit-quantised colours in a subsampled image.
    Quantising folds anti-aliasing (which would otherwise make plain text look
    colourful); subsampling keeps a 44-page deck to well under a second."""
    if img.isNull():
        return 0
    from aqt.qt import QImage
    img = img.convertToFormat(QImage.Format.Format_RGB32)
    w, h = img.width(), img.height()
    if w <= 0 or h <= 0:
        return 0
    step = max(1, min(w, h) // IMAGERY_SAMPLE_EDGE)
    seen = set()
    for y in range(0, h, step):
        for x in range(0, w, step):
            c = img.pixel(x, y)
            seen.add(((c >> 20) & 0xF, (c >> 12) & 0xF, (c >> 4) & 0xF))
    return len(seen)


# ── local OCR (soft dependency) ──────────────────────────────────────────────

def _ocr_engine() -> tuple[str, str] | None:
    """Locate a local OCR binary without ever depending on one being present.

    Returns (kind, path) or None. `tesseract` is looked for on PATH; a bundled
    `macocr` from another add-on (Practice Questions) is the Mac fallback. Both
    are soft bridges — neither is ours, neither may exist, and if nothing is
    found the caller simply leaves the segment for the vision pass.
    """
    tess = shutil.which("tesseract")
    if tess:
        return ("tesseract", tess)
    try:
        base = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
        cand = os.path.join(base, "practice_questions", "bin", "macocr")
        if os.path.exists(cand) and os.access(cand, os.X_OK):
            return ("macocr", cand)
    except Exception:
        pass
    return None


def _run_ocr(engine: tuple[str, str], image_path: str) -> str:
    kind, binp = engine
    try:
        if kind == "tesseract":
            # `stdout` sink, PSM 3 = fully automatic page segmentation (the right
            # mode for a whole slide of mixed text blocks).
            args = [binp, image_path, "stdout", "--psm", "3"]
        else:  # macocr takes just the image path and prints recognised text
            args = [binp, image_path]
        out = subprocess.run(args, capture_output=True, text=True, timeout=60)
        if out.returncode == 0:
            return out.stdout or ""
        log.error(f"lecture: {kind} exited {out.returncode}: {(out.stderr or '')[:200]}")
    except Exception as e:
        log.error(f"lecture: {kind} failed on {os.path.basename(image_path)}: {e}")
    return ""


def ocr_fill(lecture: Lecture) -> int:
    """Fill `.text` on segments that have NO text layer, using local OCR on the
    rendered page. Returns how many segments were recovered.

    Order of operations is the whole point: the text layer ALWAYS wins — only
    segments where `has_text` is false are touched, because OCR'ing a page that
    already has selectable text measurably *loses* words. This runs ahead of the
    vision pass and shrinks it: a page recovered here no longer needs an image
    sent to the model. PDF-only (nothing else rasterises); a missing OCR engine
    is a no-op, not an error — vision still catches whatever stays empty.
    """
    if not lecture.rasterisable:
        return 0
    targets = [s for s in lecture.segments if not s.has_text]
    if not targets:
        return 0
    engine = _ocr_engine()
    if engine is None:
        log.info("lecture: no local OCR engine found; leaving no-text pages for vision")
        return 0
    try:
        paths = pdf_render.render_pdf_to_images(lecture.path)
    except Exception as e:
        log.error(f"lecture: OCR render failed: {e}")
        return 0
    recovered = 0
    for s in targets:
        idx = s.number - 1
        if not (0 <= idx < len(paths)) or not os.path.exists(paths[idx]):
            continue
        text = _run_ocr(engine, paths[idx]).strip()
        if len(text) >= MIN_USABLE_CHARS:
            s.text = text
            recovered += 1
    if recovered:
        log.info(f"lecture: {engine[0]} recovered text on {recovered}/{len(targets)} "
                 "no-text page(s)")
    return recovered


def render_segment_paths(path: str, numbers: list[int]) -> dict[int, str]:
    """Rendered-page file paths for the given 1-based segment numbers.

    This is what the AI layer wants: core.api's `attachments` takes absolute FILE
    PATHS, not bytes (the CLI provider passes them to its Read tool, and the API
    providers base64 them from disk). pdf_render already renders to cached files,
    so handing paths straight over avoids a pointless bytes round-trip.

    PDF-only, like render_segment_images(). Returns only what rendered.
    """
    if not numbers or os.path.splitext(path)[1].lower() != ".pdf":
        return {}
    try:
        paths = pdf_render.render_pdf_to_images(path)
    except Exception as e:
        log.error(f"lecture: rasterise failed for {path}: {e}")
        return {}
    out: dict[int, str] = {}
    for n in numbers:
        idx = n - 1
        if 0 <= idx < len(paths) and os.path.exists(paths[idx]):
            out[n] = paths[idx]
    return out


# ── companion text (your own notes, or a transcript) ─────────────────────────
#
# Strictly supplemental. The slides are the spine of a run; this is the optional
# second document beside them — the jots you took in the lecture, your full
# written notes, or an auto-generated transcript. All three are the same thing
# mechanically (a text document that isn't the slides), so there is one slot
# rather than a "transcript feature" and a "notes feature".
#
# The unit here is a CHUNK, not a segment: one bullet, one paragraph, one thing
# you wrote down. That granularity is the whole point — a chunk is what later
# gets placed onto a single card, so it has to be card-sized. Its `index` is the
# citation contract with the model, exactly as Segment.number is for slides.

COMPANION_SUFFIXES = {".txt", ".md", ".docx", ".rtf", ".vtt", ".srt"}

# Outer sanity bound on what gets loaded at all. Generous, because a chunk that
# no card matches now costs nothing: the card-note pass sends only the chunks
# relevant to the cards in each batch, so the whole transcript being available
# is no longer the whole transcript being paid for.
COMPANION_MAX_CHARS = 200_000

# What EXTRACTION may see. That block goes into every extraction batch whole, so
# it is the one that has to stay small. A 62k transcript would otherwise be sent
# three or four times over to earn its two capped learning points.
EXTRACT_MAX_CHARS = 25_000

# Below this a "chunk" is a stray bullet marker, a page number, or a heading
# fragment — nothing that could be placed on a card.
COMPANION_MIN_CHUNK_CHARS = 15

# Cue blocks in a .vtt/.srt: "00:00:01.500 --> 00:00:04.000" with optional
# positioning suffixes, and the bare cue numbers .srt puts above each one.
_CUE_TIMING_RE = re.compile(r"-->")
_CUE_NUMBER_RE = re.compile(r"^\d+$")
# <v Dr Smith>, </v>, <00:00:02.100>, <c.colorE5E5E5> — all display markup.
_CUE_TAG_RE = re.compile(r"<[^>]*>")
# Rough sentence boundary. Good enough to group caption fragments back into
# readable runs; it is never used on text that gets written to a card verbatim.
_SENTENCE_SPLIT_RE = re.compile(r"(?<=[.!?])\s+")
_SENTENCES_PER_CHUNK = 3
# Soft size limit for a regrouped chunk. Sits under the card-note pass's own
# 400-char write limit for a reason: a chunk that arrives longer than that gets
# cut mid-word on its way onto a card, which is a poor look for text the tool
# promises to place as written.
_CHUNK_SOFT_CHARS = 320

# The same reflow, sized for a SEGMENT rather than a card note. A segment is the
# unit extraction reads at a time — a slide's worth — so a 17k transcript lands
# at roughly twenty of them instead of two unreadable walls.
TRANSCRIPT_SEGMENT_CHARS = 900

# "- ", "* ", "• ", "1. ", "2) " — a line that opens a new jot.
_BULLET_RE = re.compile(r"^\s*(?:[-*•·–—]|\d{1,2}[.)])\s+(?=\S)")
_MD_HEADING_RE = re.compile(r"^#{1,6}\s+(.*)$")

# A speaker label on its own line, as every auto-transcript exporter writes them:
# "SPEAKER 3", "Speaker 1:", "Dr Smith:". Deliberately narrow — three words at
# most — so a short line of your own notes isn't mistaken for one.
_SPEAKER_RE = re.compile(r"^(?:speaker\s*\d+|[A-Z][\w.'-]*(?:\s+[\w.'-]+){0,2})\s*:?$",
                         re.IGNORECASE)
# A paragraph longer than this is a wall of speech, not something you jotted.
_TRANSCRIPT_PARA_CHARS = 400


@dataclass
class Chunk:
    index: int           # 1-based; what the model cites back
    text: str
    context: str = ""    # the heading it sat under, if any
    # Whether the text must be written exactly as it stands. TRUE for anything
    # the student wrote: editing someone's own notes is not this tool's business,
    # and the restore-from-source rule in the card-note parser enforces it rather
    # than asking for it. FALSE for an auto-transcript, which is a machine's
    # record of a third party talking — nobody's authored words, and left raw it
    # arrives on a card full of "uh" and "OK?" and cut off mid-sentence.
    verbatim: bool = True

    def as_prompt_line(self) -> str:
        head = f"[note {self.index}]"
        if self.context:
            head += f" (under: {self.context})"
        if not self.verbatim:
            head += " (transcript — may be tidied)"
        return f"{head} {self.text}"


@dataclass
class CompanionText:
    source_name: str = ""     # filename, "pasted notes", or both
    chunks: list[Chunk] = field(default_factory=list)
    truncated: bool = False   # hit COMPANION_MAX_CHARS; tail was dropped

    def __bool__(self) -> bool:
        return bool(self.chunks)

    @property
    def total_chars(self) -> int:
        return sum(len(c.text) for c in self.chunks)

    def as_prompt_block(self, max_chars: int = EXTRACT_MAX_CHARS) -> str:
        """The form the EXTRACTOR sends to the AI, under its own header so the
        model can always tell notes from slides.

        Capped here rather than at load time, because the two consumers have
        opposite economics. Extraction sends this block whole in every batch, so
        it has to stay small. Placement picks a handful of relevant chunks per
        batch, so it can afford the full text — and truncating at load would
        deny it chunks it would never have paid for.
        """
        if not self.chunks:
            return ""
        lines = ["## Your notes"]
        used = 0
        cut = False
        for c in self.chunks:
            line = c.as_prompt_line()
            if used + len(line) + 1 > max_chars:   # +1 for the newline
                cut = True
                break
            used += len(line) + 1
            lines.append(line)
        if cut or self.truncated:
            lines.append("[note list truncated — later notes were not included]")
        return "\n".join(lines)


def _strip_cues(raw: str) -> str:
    """A .vtt/.srt into flowing prose.

    Captions arrive as thousands of two-second fragments, most of them repeated
    as the caption rolls. Left alone they chunk into unusable confetti, so the
    timings and markup come out, consecutive duplicates collapse, and what's
    left is regrouped into sentence-shaped runs.
    """
    kept: list[str] = []
    for ln in raw.splitlines():
        ln = ln.strip()
        if not ln or ln.upper().startswith("WEBVTT") or ln.startswith("NOTE "):
            continue
        if _CUE_TIMING_RE.search(ln) or _CUE_NUMBER_RE.match(ln):
            continue
        ln = _CUE_TAG_RE.sub("", ln).strip()
        # Rolling captions repeat the previous line verbatim as they scroll.
        if ln and (not kept or kept[-1] != ln):
            kept.append(ln)
    if not kept:
        return ""

    return _regroup_sentences(" ".join(kept))


def _regroup_sentences(flowing: str, target_chars: int = _CHUNK_SOFT_CHARS) -> str:
    """One long run of speech into readable paragraphs.

    Size is the whole point. As a supplementary note a chunk is what gets placed
    onto a single card, and a 1,200-character paragraph of spoken word can't be
    stapled to anything; as a primary source a chunk is a segment, the unit
    extraction reads one at a time, so `target_chars` runs larger there.
    """
    flowing = " ".join((flowing or "").split())
    if not flowing:
        return ""
    sentences = [s.strip() for s in _SENTENCE_SPLIT_RE.split(flowing) if s.strip()]
    # A YouTube auto-transcript has no punctuation at all — one of his had two
    # full stops in 17,500 characters. Sentence splitting then returns the whole
    # document as a single "sentence" and every size limit below is moot, so
    # fall back to grouping by word count. Not elegant; neither is the input.
    if len(sentences) <= 1 and len(flowing) > target_chars * 2:
        words = flowing.split()
        per = max(20, target_chars // 6)      # ~6 chars a word including the space
        return "\n\n".join(" ".join(words[i:i + per])
                           for i in range(0, len(words), per))
    if not sentences:
        return flowing
    # Sentence count alone isn't enough: three sentences of unbroken lecturing
    # runs past 800 characters, and the card-note pass would then truncate it
    # mid-word on the way onto a card. Whichever limit bites first wins.
    groups: list[str] = []
    buf: list[str] = []
    size = 0
    sentences_per = max(1, _SENTENCES_PER_CHUNK * target_chars // _CHUNK_SOFT_CHARS)
    for s in sentences:
        if buf and (len(buf) >= sentences_per
                    or size + len(s) > target_chars):
            groups.append(" ".join(buf))
            buf, size = [], 0
        buf.append(s)
        size += len(s) + 1
    if buf:
        groups.append(" ".join(buf))
    return "\n\n".join(groups)


def _looks_like_transcript(raw: str) -> bool:
    """Whether a plain .txt/.md is an auto-transcript rather than notes.

    Worth telling apart because they need opposite handling: notes are already
    in the shape you wrote them and must be left alone, while a transcript is
    undifferentiated speech that has to be cut down to card-sized pieces before
    any of it can be placed.

    Two independent signals, either sufficient, both conservative: repeated
    speaker labels, or paragraphs that are long AND unbulleted. A bulleted
    document is never a transcript, which is the guard that matters — reflowing
    someone's own notes would change text this feature promises to write
    verbatim.
    """
    text = (raw or "").strip()
    if not text:
        return False
    lines = [ln.strip() for ln in text.splitlines() if ln.strip()]
    if not lines:
        return False
    if any(_BULLET_RE.match(ln) for ln in lines):
        return False
    speakers = sum(1 for ln in lines if _SPEAKER_RE.match(ln))
    if speakers >= 3 and speakers >= len(lines) * 0.15:
        return True

    # A YouTube auto-caption: hundreds of short unpunctuated lines, wrapped
    # every few words by the caption track rather than by any sentence. One of
    # his had 512 lines averaging 7 words and TWO full stops in the whole file.
    # No other kind of document looks remotely like this.
    if len(lines) >= 40:
        words = sum(len(ln.split()) for ln in lines)
        stops = sum(text.count(c) for c in ".!?")
        if words / len(lines) <= 12 and stops <= words / 100:
            return True

    paras = [p.strip() for p in re.split(r"\n\s*\n", text) if p.strip()]
    long_paras = sum(1 for p in paras if len(p) > _TRANSCRIPT_PARA_CHARS)
    return bool(paras) and long_paras >= max(3, len(paras) * 0.5)


def _strip_speakers(raw: str) -> str:
    """Drop speaker-label lines, leaving one continuous run of speech."""
    return " ".join(ln for ln in (l.strip() for l in (raw or "").splitlines())
                    if ln and not _SPEAKER_RE.match(ln))


def _companion_raw(path: str) -> tuple[str, bool]:
    """Plain text out of a companion file, and whether it was a transcript.

    The caller can't work the second part out for itself: reflowing a transcript
    removes the speaker labels and breaks up the long paragraphs, i.e. it erases
    the very evidence it was detected by. So the answer travels with the text.
    """
    suffix = os.path.splitext(path)[1].lower()
    if suffix in (".vtt", ".srt"):
        with open(path, "r", encoding="utf-8", errors="replace") as fh:
            return _strip_cues(fh.read()), True
    if suffix == ".docx":
        # _load_docx already does the heading/table walk; flatten it back to text
        # rather than re-implementing the XML parse.
        parts: list[str] = []
        for seg in _load_docx(path):
            if seg.title.strip():
                parts.append(f"# {seg.title.strip()}")
            if seg.text.strip():
                parts.append(seg.text.strip())
        return "\n\n".join(parts), False
    with open(path, "r", encoding="utf-8", errors="replace") as fh:
        raw = fh.read()
    # A .txt/.md is usually notes, but a transcript export is one too — and left
    # as-is it chunks into unusable paragraph-sized blobs. Only reshaped when it
    # looks like speech; a bulleted document never reaches here.
    if _looks_like_transcript(raw):
        return _regroup_sentences(_strip_speakers(raw)), True
    return raw, False


def chunk_notes(raw: str) -> list[tuple[str, str]]:
    """Raw notes into (text, context) pairs, one per thing you wrote down.

    Blank lines separate blocks; inside a block, bullet markers start new jots,
    so a run of five bullets under one heading becomes five chunks and not one.
    A markdown heading becomes CONTEXT carried on the chunks beneath it — it
    helps the model work out where a jot belongs, and is never written to a card,
    because prefixing "Cranial nerves — " onto your own words would be an edit.
    """
    out: list[tuple[str, str]] = []
    context = ""
    for block in re.split(r"\n\s*\n", raw or ""):
        block = block.strip()
        if not block:
            continue

        lines = block.splitlines()
        # A block that is nothing but a heading only sets the context.
        heads = [_MD_HEADING_RE.match(ln.strip()) for ln in lines]
        if len(lines) == 1 and heads[0]:
            context = heads[0].group(1).strip()
            continue

        buf: list[str] = []

        def _flush(buf=buf):
            text = " ".join(x.strip() for x in buf if x.strip()).strip()
            buf.clear()
            if len(text) >= COMPANION_MIN_CHUNK_CHARS:
                out.append((text, context))

        for ln, head in zip(lines, heads):
            if head:
                _flush()
                context = head.group(1).strip()
                continue
            if _BULLET_RE.match(ln):
                _flush()
                ln = _BULLET_RE.sub("", ln, count=1)
            buf.append(ln)
        _flush()
    return out


def load_companion(path: str = "", pasted: str = "") -> CompanionText:
    """Your notes and/or a transcript into placeable chunks.

    Both inputs are optional and both may be given — a file of notes plus a
    couple of jots typed in the panel is a perfectly normal thing to want. Never
    raises: a companion document is supplemental, so a file that won't read
    should degrade the run to "no notes", not abort a lecture that was otherwise
    going to work.
    """
    # (text, verbatim). A file may be a transcript; anything typed into the
    # panel is the student's own words by definition.
    parts: list[tuple[str, bool]] = []
    names: list[str] = []

    if path:
        suffix = os.path.splitext(path)[1].lower()
        if suffix not in COMPANION_SUFFIXES:
            log.warn(f"lecture: companion file type {suffix or '(none)'} not supported")
        elif not os.path.exists(path):
            log.warn("lecture: companion file no longer exists")
        else:
            try:
                text, is_transcript = _companion_raw(path)
            except Exception as e:
                log.error(f"lecture: companion read failed for {path}: {e}")
                text, is_transcript = "", False
            if text.strip():
                # A transcript is nobody's authored prose, so excerpts from it
                # may be tidied on their way onto a card. A file of notes may not.
                parts.append((text, not is_transcript))
                names.append(os.path.basename(path))
            else:
                log.warn(f"lecture: companion file {os.path.basename(path)} had no text")

    if (pasted or "").strip():
        parts.append((pasted, True))
        names.append("pasted notes")

    if not parts:
        return CompanionText()

    chunks: list[Chunk] = []
    truncated = False
    used = 0
    for raw_text, verbatim in parts:
        if truncated:
            break
        for text, context in chunk_notes(raw_text):
            if used + len(text) > COMPANION_MAX_CHARS:
                truncated = True
                break
            used += len(text)
            chunks.append(Chunk(index=len(chunks) + 1, text=text,
                                context=context, verbatim=verbatim))

    if truncated:
        log.warn(f"lecture: companion text over {COMPANION_MAX_CHARS} chars — "
                 f"kept {len(chunks)} chunk(s), dropped the tail")
    log.info(f"lecture: companion text — {len(chunks)} chunk(s) from {' + '.join(names)}")
    return CompanionText(source_name=" + ".join(names), chunks=chunks, truncated=truncated)


def render_segment_images(path: str, numbers: list[int]) -> dict[int, bytes]:
    """PNG bytes for the given 1-based segment numbers, for the vision fallback.
    Returns only what rendered — a caller must tolerate missing keys.

    PDF-only: .pptx/.docx can't be rasterised without a real renderer, so the
    panel steers those users toward exporting a PDF instead.
    """
    if not numbers or os.path.splitext(path)[1].lower() != ".pdf":
        return {}
    try:
        paths = pdf_render.render_pdf_to_images(path)
    except Exception as e:
        log.error(f"lecture: rasterise failed for {path}: {e}")
        return {}

    out: dict[int, bytes] = {}
    for n in numbers:
        idx = n - 1
        if not (0 <= idx < len(paths)):
            continue
        try:
            with open(paths[idx], "rb") as fh:
                out[n] = fh.read()
        except OSError as e:
            log.warn(f"lecture: couldn't read rendered segment {n}: {e}")
    return out
