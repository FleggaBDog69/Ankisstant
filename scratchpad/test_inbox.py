"""Inbox and patch drains: claim the file, never lose a late append.

Runs the real store and patch_inbox code against a temp user_files folder —
never the live one."""
import importlib.util, json, os, sys, tempfile, types

ADDON = __file__.rsplit("/scratchpad/", 1)[0]
def shell(name, path=None):
    m = types.ModuleType(name)
    if path:
        m.__path__ = [path]
    sys.modules[name] = m
    return m
shell("ank", ADDON); shell("ank.core", ADDON + "/core")
shell("ank.tools", ADDON + "/tools"); shell("ank.tools.kg", ADDON + "/tools/kg")
log = shell("ank.core.log")
log.error = log.warn = log.info = log.trace = lambda *a, **k: None
def load(name, rel):
    spec = importlib.util.spec_from_file_location(name, ADDON + rel)
    m = importlib.util.module_from_spec(spec); sys.modules[name] = m
    spec.loader.exec_module(m); return m
load("ank.core.jsonfile", "/core/jsonfile.py")
store = load("ank.tools.kg.store", "/tools/kg/store.py")
patch = load("ank.tools.kg.patch_inbox", "/tools/kg/patch_inbox.py")

tmp = tempfile.mkdtemp()
store._user_files = lambda: tmp
patch_dir = tmp
patch._patch_path = lambda: os.path.join(tmp, "kg_patches.json")
patch._result_path = lambda: os.path.join(tmp, "kg_patches.result.json")

fails = []
def check(name, got, want):
    ok = got == want
    print(("PASS  " if ok else "FAIL  ") + name)
    if not ok:
        print(f"      got  {got!r}\n      want {want!r}"); fails.append(name)

inbox = os.path.join(tmp, "kg_inbox.json")
def put(path, rows):
    with open(path, "w") as f:
        json.dump(rows, f)

put(inbox, [{"title": "Mirizzi syndrome", "group": "SRS 12"}])
check("a drain adds what's in the inbox", store.drain_inbox(), 1)
check("…and leaves no inbox or claim behind",
      sorted(n for n in os.listdir(tmp) if "inbox" in n), [])

# The race: something appends WHILE the drain is working. Simulated by
# appending from inside add_many, i.e. after the inbox was read.
put(inbox, [{"title": "Charcot's triad"}])
real_add_many = store.add_many
def add_many_and_race(items, *a, **k):
    put(inbox, [{"title": "Reynolds' pentad"}])   # a writer starts a fresh inbox
    return real_add_many(items, *a, **k)
store.add_many = add_many_and_race
store.drain_inbox()
store.add_many = real_add_many
check("an append made mid-drain is still waiting",
      json.load(open(inbox)), [{"title": "Reynolds' pentad"}])
store.drain_inbox()
titles = sorted(it["title"] for it in store.load_all())
check("…and lands on the next drain",
      titles, ["Charcot's triad", "Mirizzi syndrome", "Reynolds' pentad"])

# A drain that died after claiming: its claim is finished next time.
put(inbox + ".draining", [{"title": "Courvoisier's law"}])
check("a leftover claim is finished first", store.drain_inbox(), 1)

# Patches: same claim, and a patch actually applies.
gid = next(it["id"] for it in store.load_all() if it["title"] == "Mirizzi syndrome")
put(patch._patch_path(), [{"id": gid, "status": "done"}])
res = patch.apply_pending()
check("a patch applies", [r.get("ok") for r in res], [True])
check("…the gap is done", store.get(gid)["status"], "done")
check("…and the patch file is gone", os.path.exists(patch._patch_path()), False)

print()
print("FAILED: " + ", ".join(fails) if fails else "all green")
sys.exit(1 if fails else 0)
