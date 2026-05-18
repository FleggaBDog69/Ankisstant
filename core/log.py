# Tiny logger so the rest of the addon doesn't pepper stderr with prints.
# Set debug_logging=true in the addon config (Anki: Tools → Add-ons → Config)
# to see info()/debug() output; warn()/error() always print.

from __future__ import annotations

import sys

_PREFIX = "[ankisstant]"


def _debug_enabled() -> bool:
    # Imported lazily to avoid a circular import (config.py uses nothing else
    # from this module, but keeps the dependency one-way).
    try:
        from .config import load_config
        return bool(load_config().get("debug_logging", False))
    except Exception:
        return False


def info(msg: str) -> None:
    if _debug_enabled():
        print(f"{_PREFIX} {msg}", file=sys.stderr)


def debug(msg: str) -> None:
    if _debug_enabled():
        print(f"{_PREFIX} debug: {msg}", file=sys.stderr)


def warn(msg: str) -> None:
    print(f"{_PREFIX} warn: {msg}", file=sys.stderr)


def error(msg: str) -> None:
    print(f"{_PREFIX} error: {msg}", file=sys.stderr)
