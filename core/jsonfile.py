# Safe JSON files for the add-on's own stores (gaps, jobs, ticks, the queue).
#
# Two failures this exists to stop, and they compound:
#
#   1. open(path, "w") truncates first. Anki quitting, crashing or the Mac
#      sleeping between the truncate and the last byte leaves an empty or
#      half-written file.
#   2. Every loader treated a file it couldn't parse as "no data" and returned
#      []. The next save then wrote [] + whatever was added — so one bad write
#      became every gap gone, silently.
#
# write() replaces the file atomically (temp file in the same folder, flushed
# to disk, then os.replace), so a reader only ever sees the old file or the
# new one. read() moves an unparseable file aside under a dated name before
# reporting "no data", so it can never be overwritten and can be recovered.

from __future__ import annotations

import json
import os
import time

from . import log


def write(path: str, data, **dump_kwargs) -> bool:
    """Atomically write `data` as JSON. Returns False (and logs) on failure,
    leaving whatever was on disk untouched."""
    tmp = f"{path}.tmp-{os.getpid()}"
    try:
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(data, f, **dump_kwargs)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
        return True
    except (OSError, TypeError, ValueError) as e:
        log.error(f"couldn't save {os.path.basename(path)}: {e}")
        try:
            os.remove(tmp)
        except OSError:
            pass
        return False


def read(path: str, default):
    """Load JSON from `path`. Missing file -> `default`. A file that exists but
    won't parse is renamed to `<name>.corrupt-<timestamp>` first, then
    `default` is returned — the data is set aside, never overwritten."""
    if not os.path.exists(path):
        return default
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except json.JSONDecodeError as e:
        aside = f"{path}.corrupt-{time.strftime('%Y%m%d-%H%M%S')}"
        try:
            os.replace(path, aside)
            log.error(f"{os.path.basename(path)} couldn't be read ({e}); "
                      f"moved aside to {os.path.basename(aside)}")
        except OSError as e2:
            log.error(f"{os.path.basename(path)} couldn't be read ({e}) "
                      f"or moved aside ({e2})")
        return default
    except OSError as e:
        log.error(f"couldn't open {os.path.basename(path)}: {e}")
        return default
