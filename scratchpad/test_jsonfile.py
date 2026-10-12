"""core/jsonfile: atomic writes, and a corrupt store set aside, not wiped."""
import importlib.util, os, sys, tempfile, types

ADDON = __file__.rsplit("/scratchpad/", 1)[0]
pkg = types.ModuleType("ank"); pkg.__path__ = [ADDON]; sys.modules["ank"] = pkg
core = types.ModuleType("ank.core"); core.__path__ = [ADDON + "/core"]; sys.modules["ank.core"] = core
log = types.ModuleType("ank.core.log")
log.error = log.warn = lambda *a, **k: None
sys.modules["ank.core.log"] = log; core.log = log
spec = importlib.util.spec_from_file_location("ank.core.jsonfile", ADDON + "/core/jsonfile.py")
jf = importlib.util.module_from_spec(spec); sys.modules[spec.name] = jf; spec.loader.exec_module(jf)

fails = []
def check(name, got, want):
    ok = got == want
    print(("PASS  " if ok else "FAIL  ") + name)
    if not ok:
        print(f"      got  {got!r}\n      want {want!r}"); fails.append(name)

d = tempfile.mkdtemp()
p = os.path.join(d, "kg_queue.json")

check("missing file reads as the default", jf.read(p, []), [])
check("write succeeds", jf.write(p, [{"id": "a"}], indent=2), True)
check("round trip", jf.read(p, []), [{"id": "a"}])
check("no temp file left behind", sorted(os.listdir(d)), ["kg_queue.json"])

# A half-written file — what a crash mid-save used to leave.
with open(p, "w") as f:
    f.write('[{"id": "a"}, {"id": "b", "title": "half')
check("a corrupt store reads as the default", jf.read(p, []), [])
aside = [n for n in os.listdir(d) if n.startswith("kg_queue.json.corrupt-")]
check("…and is set aside, not lost", len(aside), 1)
with open(os.path.join(d, aside[0])) as f:
    check("…with its contents intact", f.read().startswith('[{"id": "a"}'), True)
check("the next save can't overwrite it", jf.write(p, [{"id": "new"}]), True)
check("…the set-aside copy is still there",
      len([n for n in os.listdir(d) if n.startswith("kg_queue.json.corrupt-")]), 1)

# A value json can't encode must not truncate what's on disk.
jf.write(p, [{"id": "keep"}])
check("an unencodable save reports failure", jf.write(p, [{"x": object()}]), False)
check("…and leaves the old file intact", jf.read(p, []), [{"id": "keep"}])

print()
print("FAILED: " + ", ".join(fails) if fails else "all green")
sys.exit(1 if fails else 0)
