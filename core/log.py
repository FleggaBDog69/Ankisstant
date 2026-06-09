# Tiny logger so the rest of the addon doesn't pepper stderr with prints.
#
# IMPORTANT: Anki installs a hook on sys.stderr — *any* write to stderr pops its
# global "An error occurred" dialog (with the full add-on list). A handled,
# recoverable failure (e.g. a transient Gemini 503 that ask_claude already
# catches and turns into a tooltip) must therefore NOT go to stderr, or the user
# sees what looks like a crash. So all levels write to a rotating log file in
# user_files/ instead. They mirror to stderr ONLY when debug_logging=true is set
# in the addon config (Tools → Add-ons → Ankisstant → Config), which is the
# explicit "I want the crash-looking dialog for diagnostics" opt-in.

from __future__ import annotations

import os
import sys
import threading
import time

_PREFIX = "[ankisstant]"

# Serialise file writes — log() is called from background worker threads
# (taskman) as well as the main thread.
_lock = threading.Lock()
_LOG_PATH: str | None = None
_MAX_BYTES = 512 * 1024  # rotate at ~0.5 MB; keep one .1 backup


def _log_path() -> str | None:
    """Absolute path to user_files/ankisstant.log, or None if it can't be
    resolved (in which case file logging is silently skipped)."""
    global _LOG_PATH
    if _LOG_PATH is not None:
        return _LOG_PATH
    try:
        # core/log.py -> core/ -> addon root -> user_files/
        root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        user_files = os.path.join(root, "user_files")
        os.makedirs(user_files, exist_ok=True)
        _LOG_PATH = os.path.join(user_files, "ankisstant.log")
    except Exception:
        _LOG_PATH = None
    return _LOG_PATH


def _debug_enabled() -> bool:
    # Imported lazily to avoid a circular import (config.py uses nothing else
    # from this module, but keeps the dependency one-way).
    try:
        from .config import load_config
        return bool(load_config().get("debug_logging", False))
    except Exception:
        return False


def _emit(level: str, msg: str) -> None:
    line = f"{_PREFIX} {level}{msg}" if level else f"{_PREFIX} {msg}"
    # Mirror to stderr only when the user opted into debug logging — this is the
    # only path that can trigger Anki's error dialog, and now it's intentional.
    if _debug_enabled():
        print(line, file=sys.stderr)
    path = _log_path()
    if not path:
        return
    stamp = time.strftime("%Y-%m-%d %H:%M:%S")
    try:
        with _lock:
            try:
                if os.path.getsize(path) > _MAX_BYTES:
                    os.replace(path, path + ".1")
            except OSError:
                pass
            with open(path, "a", encoding="utf-8") as f:
                f.write(f"{stamp} {line}\n")
    except Exception:
        pass  # logging must never raise


def info(msg: str) -> None:
    if _debug_enabled():
        _emit("", msg)


def debug(msg: str) -> None:
    if _debug_enabled():
        _emit("debug: ", msg)


def warn(msg: str) -> None:
    _emit("warn: ", msg)


def error(msg: str) -> None:
    _emit("error: ", msg)
