# AI Lecture — find the good cards you already own for a lecture, and unsuspend
# them. Retrieval, not generation.
#
# This file is deliberately a thin shim. The engine (ingest/extractor/matcher)
# imports no Qt and knows nothing about threads, which is what lets it be run and
# measured in plain Python outside Anki — several of its non-obvious bugs were
# only ever caught that way. Importing .panel here at module scope would drag Qt
# into that path and end it, so the import is deferred into the entry points.

from __future__ import annotations

NAME = "AI Lecture"


def init(main_window) -> None:
    from .panel import init as _init
    return _init(main_window)


def get_panel():
    from .panel import get_panel as _get_panel
    return _get_panel()
