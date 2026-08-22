"""Image import for the slide-attach feature.

Vendored into the lecture package so it has NO dependency on the AI Create tool
(card_creator): the standalone LectureLens build ships without card_creator, but
slide-attach must still work. Lifted verbatim from card_creator's
`_normalize_image_file` / `_import_image_to_media` — the only image helper
slide-attach actually calls. Never raises: on any problem the image is imported
as-is, or skipped, so a commit is never sunk by one bad picture."""
from __future__ import annotations

import os
import tempfile

from aqt import mw
from aqt.qt import QImage, Qt

from ...core import log

_IMG_MAX_DIM = 1600              # px on the longest side
_IMG_REENCODE_BYTES = 1_500_000  # larger than this gets re-encoded even if small-dim


def _normalize_image_file(path: str) -> str:
    """Return a path to a web-safe (downscaled, re-encoded) copy of `path` when
    it is oversized; otherwise return `path` unchanged. Never raises — on any
    problem the original path is returned so attachment still works."""
    try:
        if not path or not os.path.exists(path):
            return path
        try:
            size = os.path.getsize(path)
        except OSError:
            size = 0
        img = QImage(path)
        if img.isNull():
            return path  # not a raster Qt understands (e.g. SVG) — leave as-is
        w, h = img.width(), img.height()
        longest = max(w, h)
        if longest <= _IMG_MAX_DIM and size <= _IMG_REENCODE_BYTES:
            return path  # already small enough; keep original bytes/quality
        if longest > _IMG_MAX_DIM:
            img = img.scaled(
                _IMG_MAX_DIM, _IMG_MAX_DIM,
                Qt.AspectRatioMode.KeepAspectRatio,
                Qt.TransformationMode.SmoothTransformation,
            )
        has_alpha = img.hasAlphaChannel()
        ext = ".png" if has_alpha else ".jpg"
        fd, out = tempfile.mkstemp(prefix="lecture_img_norm_", suffix=ext)
        os.close(fd)
        ok = img.save(out, "PNG" if has_alpha else "JPG", -1 if has_alpha else 85)
        if ok and os.path.getsize(out) > 0:
            return out
        try:
            os.remove(out)
        except OSError:
            pass
        return path
    except Exception as e:
        log.warn(f"lecture: image normalize failed for {path}: {e}")
        return path


def import_image_to_media(path: str) -> str | None:
    """Copy an image into Anki's media folder; return the stored filename
    (which may differ from the original basename on collision). Oversized images
    are downscaled first to keep things responsive. None on failure."""
    try:
        return mw.col.media.add_file(_normalize_image_file(path))
    except Exception as e:
        log.warn(f"lecture: media.add_file failed for {path}: {e}")
        return None
