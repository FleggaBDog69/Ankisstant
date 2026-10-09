"""Load ankisstant.tools.lecture.matcher without Anki, as a package shell."""
import sys, types
ADDON = "/Users/fletcherbucat/Library/Application Support/Anki2/addons21/ankisstant"

def shell(name, path=None):
    m = types.ModuleType(name)
    if path:
        m.__path__ = [path]
    sys.modules[name] = m
    return m

shell("ank", ADDON)
shell("ank.tools", ADDON + "/tools")
shell("ank.lecture_pkg")
shell("ank.core", ADDON + "/core")

log = shell("ank.core.log")
log.enabled = lambda: False
for fn in ("trace", "warn", "error", "info"):
    setattr(log, fn, lambda *a, **k: None)

api = shell("ank.core.api")
class ClaudeCancelled(Exception): pass
class ClaudeError(Exception): pass
api.ClaudeCancelled = ClaudeCancelled
api.ClaudeError = ClaudeError
api.ask_claude_json = lambda **k: None
api.classify_ai_error = lambda s: ("error", s)

cfg = shell("ank.core.config")
cfg.tool_config = lambda t: {}
cfg.auto_tag_base = lambda: "!!Fleg"
cfg.format_hierarchical_tag = lambda *a, **k: ""
cfg.content_segment = lambda: "KG"

aqt = shell("aqt", "/nonexistent")
aqt.mw = None
utils = shell("aqt.utils")
utils.tooltip = lambda *a, **k: None
utils.showWarning = lambda *a, **k: None
utils.showInfo = lambda *a, **k: None
utils.askUser = lambda *a, **k: False
qt = shell("aqt.qt")
def _any(name):
    if name.startswith("__"):
        raise AttributeError(name)
    return type(name, (), {"__getattr__": lambda self, n: None})
qt.__getattr__ = _any
anki = shell("anki", "/nonexistent")
for sub in ("notes", "cards", "collection", "utils", "hooks", "consts",
            "models", "decks", "tags", "errors", "lang"):
    shell("anki." + sub).__getattr__ = _any
anki.__getattr__ = _any
utils.__getattr__ = _any

import importlib
matcher = importlib.import_module("ank.tools.lecture.matcher")
