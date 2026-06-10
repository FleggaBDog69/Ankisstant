# PDF → per-page image rasteriser, built on Anki's bundled Qt PDF module
# (PyQt6.QtPdf). No third-party dependency — QtPdf ships with the PyQt6 that
# Anki bundles on every platform.
#
# Used by AI Create to (a) feed page-images to vision providers that can't read
# PDFs directly (Gemini/OpenAI/Ollama), and (b) let the user attach the actual
# source slide image to each generated card.
#
# Defensive by design: every entry point swallows failures and returns an empty
# list rather than raising, so a malformed PDF or a Qt quirk never breaks card
# generation — the cards still get made, just without slide images.

from __future__ import annotations

import os
import tempfile

from aqt.qt import QSize

from ..core import log


# A render cache keyed by (abspath, mtime, long_edge, max_pages). PDFs are
# attached once and rendered once per generate; this avoids re-rasterising the
# same deck when the review dialog and the slide gallery both ask for pages.
_CACHE: dict[tuple, list[str]] = {}

DEFAULT_LONG_EDGE = 1280   # px on the long side — sharp enough for a slide, cheap to send
DEFAULT_MAX_PAGES = 80     # hard cap so a giant PDF can't hang Anki or blow up token cost


def _cache_key(path: str, long_edge: int, max_pages: int) -> tuple | None:
    try:
        ap = os.path.abspath(path)
        return (ap, os.path.getmtime(ap), long_edge, max_pages)
    except OSError:
        return None


def render_pdf_to_images(
    pdf_path: str,
    *,
    long_edge: int = DEFAULT_LONG_EDGE,
    max_pages: int = DEFAULT_MAX_PAGES,
) -> list[str]:
    """Render each page of `pdf_path` to a temp PNG and return the paths in
    page order (index 0 == page 1). Returns [] on any failure. At most
    `max_pages` pages are rendered; callers can compare len() against
    page_count() to detect truncation."""
    key = _cache_key(pdf_path, long_edge, max_pages)
    if key is not None:
        cached = _CACHE.get(key)
        if cached and all(os.path.exists(p) for p in cached):
            return cached

    try:
        # Imported lazily so importing this module never fails on a Qt build
        # that somehow lacks QtPdf — callers just get [].
        from aqt.qt import QImage  # noqa: F401  (sanity that Qt GUI is available)
        from PyQt6.QtPdf import QPdfDocument, QPdfDocumentRenderOptions  # type: ignore
    except Exception as e:
        log.error(f"pdf_render: QtPdf unavailable: {e}")
        return []

    doc = QPdfDocument(None)
    try:
        err = doc.load(pdf_path)
        if err != QPdfDocument.Error.None_:
            log.error(f"pdf_render: load failed for {pdf_path}: {err}")
            return []

        count = doc.pageCount()
        if count <= 0:
            return []
        n = min(count, max_pages)

        # Render into a dedicated temp dir with zero-padded, page-ordered names
        # (slide_0001.png …). Ordered names mean that anything which later
        # round-trips the files by basename (e.g. a queued job copying them into
        # its folder) can recover page order with a plain sort.
        out_dir = tempfile.mkdtemp(prefix="ankisstant_slides_")
        opts = QPdfDocumentRenderOptions()
        out: list[str] = []
        for i in range(n):
            size = _page_pixel_size(doc, i, long_edge)
            img = doc.render(i, size, opts)
            if img.isNull():
                log.error(f"pdf_render: null image for page {i} of {pdf_path}")
                continue
            tmp = os.path.join(out_dir, f"slide_{i + 1:04d}.png")
            if img.save(tmp, "PNG") and os.path.getsize(tmp) > 0:
                out.append(tmp)
        if key is not None and out:
            _CACHE[key] = out
        return out
    except Exception as e:
        log.error(f"pdf_render: render failed for {pdf_path}: {e}")
        return []
    finally:
        try:
            doc.close()
        except Exception:
            pass


# A per-page text cache keyed by (abspath, mtime). Text extraction is cheap, but
# a single generate may ask for it more than once (dispatch + worker match), so
# we memoise alongside the render cache.
_TEXT_CACHE: dict[tuple, list[str]] = {}


def extract_pdf_text_per_page(pdf_path: str) -> list[str]:
    """Return the selectable text of each page, in page order (index 0 == page
    1), using the SAME QtPdf module the rasteriser uses — no third-party
    dependency. A page with no extractable text (image-only slide) yields "".
    Returns [] on any failure, so a caller can compare total text length to
    decide whether a text-first pass is viable or it must fall back to vision."""
    try:
        ap = os.path.abspath(pdf_path)
        key = (ap, os.path.getmtime(ap))
    except OSError:
        key = None
    if key is not None and key in _TEXT_CACHE:
        return _TEXT_CACHE[key]

    try:
        from PyQt6.QtPdf import QPdfDocument  # type: ignore
    except Exception as e:
        log.error(f"pdf_render: QtPdf unavailable for text: {e}")
        return []

    doc = QPdfDocument(None)
    try:
        if doc.load(pdf_path) != QPdfDocument.Error.None_:
            log.error(f"pdf_render: text load failed for {pdf_path}")
            return []
        count = doc.pageCount()
        if count <= 0:
            return []
        out: list[str] = []
        for i in range(count):
            try:
                sel = doc.getAllText(i)
                out.append(sel.text() if sel is not None else "")
            except Exception as e:
                log.error(f"pdf_render: getAllText failed on page {i}: {e}")
                out.append("")
        if key is not None:
            _TEXT_CACHE[key] = out
        return out
    except Exception as e:
        log.error(f"pdf_render: text extract failed for {pdf_path}: {e}")
        return []
    finally:
        try:
            doc.close()
        except Exception:
            pass


def page_count(pdf_path: str) -> int:
    """Number of pages in the PDF, or 0 if it can't be read."""
    try:
        from PyQt6.QtPdf import QPdfDocument  # type: ignore
    except Exception:
        return 0
    doc = QPdfDocument(None)
    try:
        if doc.load(pdf_path) != QPdfDocument.Error.None_:
            return 0
        return max(0, doc.pageCount())
    except Exception:
        return 0
    finally:
        try:
            doc.close()
        except Exception:
            pass


def _page_pixel_size(doc, page: int, long_edge: int) -> QSize:
    """Pixel render size that fits the page's aspect ratio into a `long_edge`
    box. Falls back to a 4:3 slide if the point size is unavailable."""
    try:
        pt = doc.pagePointSize(page)  # points (1/72 inch) → gives aspect ratio
        w, h = pt.width(), pt.height()
        if w <= 0 or h <= 0:
            raise ValueError
    except Exception:
        w, h = 4.0, 3.0
    if w >= h:
        rw = long_edge
        rh = max(1, round(long_edge * h / w))
    else:
        rh = long_edge
        rw = max(1, round(long_edge * w / h))
    return QSize(int(rw), int(rh))
