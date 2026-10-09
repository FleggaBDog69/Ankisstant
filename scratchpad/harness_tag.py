"""Load ankisstant.tools.autotag on top of the REAL core/config.py, with a
fake mw whose addon config is the one this profile actually has."""
import sys, types, json
ADDON = "/Users/fletcherbucat/Library/Application Support/Anki2/addons21/ankisstant"

def shell(name, path=None):
    m = types.ModuleType(name)
    if path:
        m.__path__ = [path]
    sys.modules[name] = m
    return m

def _any(name):
    if name.startswith("__"):
        raise AttributeError(name)
    return type(name, (), {"__getattr__": lambda self, n: None})

shell("ank", ADDON)
shell("ank.tools", ADDON + "/tools")
shell("ank.core", ADDON + "/core")
log = shell("ank.core.log")
log.enabled = lambda: False
for fn in ("trace", "warn", "error", "info"):
    setattr(log, fn, lambda *a, **k: None)
api = shell("ank.core.api")
api.__getattr__ = _any

aqt = shell("aqt", "/nonexistent")
aqt.__getattr__ = _any
for sub in ("utils", "qt", "webview", "operations"):
    shell("aqt." + sub).__getattr__ = _any
anki = shell("anki", "/nonexistent")
anki.__getattr__ = _any
for sub in ("notes", "cards", "collection", "utils", "hooks", "consts", "tags"):
    shell("anki." + sub).__getattr__ = _any

CONFIG = json.load(open(ADDON + "/meta.json"))["config"]

class _AM:
    def getConfig(self, _pkg): return CONFIG
    def writeConfig(self, _pkg, cfg): CONFIG.clear(); CONFIG.update(cfg)
class _Tags:
    def all(self): return []
class _Col:
    tags = _Tags()
    def find_notes(self, q): return []
class _MW:
    addonManager = _AM()
    col = _Col()
    pm = None
aqt.mw = _MW()

import importlib
config = importlib.import_module("ank.core.config")
config.ADDON = "ankisstant"
autotag = importlib.import_module("ank.tools.autotag")
