# Screenshots, pasted text and web pages, as a lecture.
#
# AI Create used to take these directly. With AI Create gone as a page, they
# come in through AI Lecture instead, and the cheapest faithful way to do that
# is to bind them into ONE PDF and hand it to the lecture reader unchanged:
#
#   - pasted text and the web page's text become ordinary text pages, so they
#     are read exactly like a handout;
#   - each screenshot becomes a page of its own with no text layer, so the
#     reader's local OCR (and vision, if it's on) reads it, imagery detection
#     sees its pictures, and the page is stapled onto the cards it taught —
#     the same as a slide.
#
# Nothing downstream needs to know the PDF was made here. The file is named by
# a hash of its content, so running the same material twice reuses both the
# file and the reader's cache.

from __future__ import annotations

import hashlib
import os
import re
import uuid

from ...core import anki_utils, log


def material_dir() -> str:
    here = os.path.dirname(os.path.abspath(__file__))
    root = os.path.dirname(os.path.dirname(here))
    d = os.path.join(root, "user_files", "lecture_material")
    os.makedirs(d, exist_ok=True)
    return d


def save_clipboard_image() -> str | None:
    """The image on the clipboard, saved as a PNG. None if there isn't one."""
    from aqt.qt import QApplication
    img = QApplication.clipboard().image()
    if img is None or img.isNull():
        return None
    shots = os.path.join(material_dir(), "shots")
    os.makedirs(shots, exist_ok=True)
    path = os.path.join(shots, f"{uuid.uuid4().hex[:12]}.png")
    if not img.save(path, "PNG"):
        return None
    return path


def fetch_url_text(url: str) -> str:
    """A web page's readable text. Raises on a network failure — the caller
    says so rather than reading nothing."""
    from ..card_creator import _fetch_url
    return _fetch_url(url)


def default_title(text: str, url: str, n_images: int) -> str:
    if url:
        host = re.sub(r"^https?://(www\.)?", "", url).split("/")[0]
        if host:
            return host
    for line in (text or "").splitlines():
        line = line.strip(" #*-•\t")
        if len(line) >= 4:
            return line[:60]
    if n_images:
        return f"{n_images} screenshot{'s' if n_images != 1 else ''}"
    return "Material"


def _paragraphs(text: str, limit: int = 1200) -> list[str]:
    """Lines, with any single line too long for a page broken at word
    boundaries — a pasted web page is often one enormous line."""
    out: list[str] = []
    for line in text.split("\n"):
        while len(line) > limit:
            cut = line.rfind(" ", 0, limit)
            cut = cut if cut > limit // 2 else limit
            out.append(line[:cut])
            line = line[cut:].lstrip()
        out.append(line)
    return out


def build_pdf(title: str, text: str, images: list[str]) -> str | None:
    """Bind the text and screenshots into one PDF. Returns its path, or None
    when there was nothing to bind."""
    text = (text or "").strip()
    images = [p for p in (images or []) if p and os.path.exists(p)]
    if not text and not images:
        return None

    h = hashlib.sha1()
    h.update(text.encode("utf-8"))
    for p in images:
        with open(p, "rb") as f:
            h.update(f.read())
    leaf = anki_utils.slugify_tag_leaf(title or "material") or "material"
    path = os.path.join(material_dir(), f"{leaf}-{h.hexdigest()[:10]}.pdf")
    if os.path.exists(path):
        return path         # same material: same file, so the read cache hits

    from aqt.qt import (QFont, QImage, QMarginsF, QPageLayout, QPageSize,
                        QPainter, QPdfWriter, QRectF, QTextDocument)
    writer = QPdfWriter(path)
    writer.setTitle(title or "Material")
    writer.setResolution(150)
    writer.setPageSize(QPageSize(QPageSize.PageSizeId.A4))
    writer.setPageMargins(QMarginsF(15, 15, 15, 15), QPageLayout.Unit.Millimeter)
    rect = writer.pageLayout().paintRectPixels(writer.resolution())
    w, hgt = float(rect.width()), float(rect.height())

    painter = QPainter(writer)
    first = True
    try:
        if text:
            # One document PER PAGE, filled a paragraph at a time. Drawing one
            # long document through a moving clip window looks right but isn't:
            # the clipped-off line at each page break is still written into
            # the PDF, so the reader would read it twice.
            def _doc(body: str):
                d = QTextDocument()
                # Lay out at the PDF's resolution, not the screen's, or the
                # text comes out at a fraction of its size.
                d.documentLayout().setPaintDevice(writer)
                d.setDefaultFont(QFont("Helvetica", 11))
                d.setDocumentMargin(0)
                d.setTextWidth(w)
                d.setPlainText(body)
                return d

            pages: list[str] = []
            cur: list[str] = []
            for para in _paragraphs(text):
                trial = "\n".join(cur + [para])
                if cur and _doc(trial).size().height() > hgt:
                    pages.append("\n".join(cur))
                    cur = [para]
                else:
                    cur.append(para)
            if cur:
                pages.append("\n".join(cur))
            for body in pages:
                if not first:
                    writer.newPage()
                first = False
                _doc(body).drawContents(painter, QRectF(0, 0, w, hgt))
        for p in images:
            img = QImage(p)
            if img.isNull():
                log.warn(f"lecture material: couldn't read image {p}")
                continue
            if not first:
                writer.newPage()
            first = False
            scale = min(w / img.width(), hgt / img.height())
            tw, th = img.width() * scale, img.height() * scale
            painter.drawImage(QRectF((w - tw) / 2, 0, tw, th), img)
    finally:
        painter.end()
    if first:
        # Every image failed to load and there was no text.
        try:
            os.remove(path)
        except OSError:
            pass
        return None
    return path
