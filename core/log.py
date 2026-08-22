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
    # `debug_logging` is the "mirror to stderr" opt-in ONLY — the one path that
    # pops Anki's crash dialog. Keep it separate from file logging so a chatty
    # diagnostic run doesn't spray fake "An error occurred" dialogs.
    # Imported lazily to avoid a circular import (config.py uses nothing else
    # from this module, but keeps the dependency one-way).
    try:
        from .config import load_config
        return bool(load_config().get("debug_logging", False))
    except Exception:
        return False


def _file_enabled() -> bool:
    """Whether to write the log FILE (trace/info/debug). Independent of stderr
    mirroring: set `file_logging=true` to capture the full diagnostic trail in
    user_files/ankisstant.log WITHOUT the crash-looking dialogs that
    `debug_logging` causes. Falls back to `debug_logging` when unset so nothing
    changes for anyone who never touches the new key."""
    try:
        from .config import load_config
        c = load_config()
        if "file_logging" in c:
            return bool(c.get("file_logging"))
        return bool(c.get("debug_logging", False))
    except Exception:
        return False


def _write_file(line: str) -> None:
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


def _emit(level: str, msg: str, *, always_file: bool = False) -> None:
    line = f"{_PREFIX} {level}{msg}" if level else f"{_PREFIX} {msg}"
    # Mirror to stderr only when the user opted into debug logging — this is the
    # only path that can trigger Anki's error dialog, and now it's intentional.
    if _debug_enabled():
        print(line, file=sys.stderr)
    # warn()/error() always persist; info()/debug() persist when file logging is on.
    if always_file or _file_enabled():
        _write_file(line)


def enabled() -> bool:
    """Whether any diagnostic logging is on — so callers can skip building an
    expensive diagnostic payload (e.g. loading note text) when nobody will read
    it. True when EITHER the file trail or the stderr mirror is enabled."""
    return _debug_enabled() or _file_enabled()


def trace(msg: str) -> None:
    """Verbose per-run diagnostic detail. Writes to the log FILE only, and NEVER
    mirrors to stderr — so a chatty trail (the lecture pipeline's per-point
    candidate dump) can't trip Anki's error dialog the way info()/warn() do.
    Gated on file logging so normal runs don't bloat the log."""
    if _file_enabled():
        _write_file(f"{_PREFIX} trace: {msg}")


def info(msg: str) -> None:
    if _debug_enabled() or _file_enabled():
        _emit("", msg)


def debug(msg: str) -> None:
    if _debug_enabled() or _file_enabled():
        _emit("debug: ", msg)


def warn(msg: str) -> None:
    _emit("warn: ", msg, always_file=True)


def error(msg: str) -> None:
    _emit("error: ", msg, always_file=True)
