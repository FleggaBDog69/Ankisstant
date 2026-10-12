"""Config migrations against a config saved before the MQ explanation field
existed. A synthetic config, not meta.json — that holds API keys."""
import copy, importlib.util, sys, types

ADDON = __file__.rsplit("/scratchpad/", 1)[0]
aqt = types.ModuleType("aqt"); aqt.mw = None; sys.modules["aqt"] = aqt
spec = importlib.util.spec_from_file_location("cfgmod", ADDON + "/core/config.py")
c = importlib.util.module_from_spec(spec); sys.modules["cfgmod"] = c; spec.loader.exec_module(c)
spec = importlib.util.spec_from_file_location("engine", ADDON + "/tools/kg/engine.py")
e = importlib.util.module_from_spec(spec); sys.modules["engine"] = e; spec.loader.exec_module(e)

fails = []
def check(name, got, want):
    ok = got == want
    print(("PASS  " if ok else "FAIL  ") + name)
    if not ok:
        print(f"      got  {got!r}\n      want {want!r}")
        fails.append(name)

def legacy():
    """The type list as 1.7 saved it: no explanation field."""
    return {"tools": {"knowledge_gaps": {"types": [
        {"key": "mq", "name": "MQ", "fields": [
            {"key": k, "label": k, "kind": "text"} for k in
            ("concept", "stem_html", "system", "subsystem", "topic", "platform", "notes")]},
        {"key": "kg", "name": "KG", "fields": [{"key": "notes", "label": "Notes", "kind": "longtext"}]},
    ]}}}

def mq_of(cfg):
    return next(t for t in cfg["tools"]["knowledge_gaps"]["types"] if t["key"] == "mq")

def keys(t):
    return [f["key"] for f in t["fields"]]

cfg = c._deep_merge(c.DEFAULTS, legacy())
plan, _ = e.plan_for(mq_of(cfg), "create", want_cards=True, want_terms=False, want_tag=True)
check("before: a legacy MQ type asks for no explanation", plan.note_fields, [])

c._migrate_mq_explanation_field(cfg)
check("explanation lands after concept", keys(mq_of(cfg))[:3], ["concept", "explanation", "stem_html"])
check("the flag is set", cfg["mq_explanation_field_migrated"], True)
plan, _ = e.plan_for(mq_of(cfg), "create", want_cards=True, want_terms=False, want_tag=True)
check("after: the create call asks for it", plan.note_fields, ["explanation"])

mq_of(cfg)["fields"] = [f for f in mq_of(cfg)["fields"] if f["key"] != "explanation"]
c._migrate_mq_explanation_field(cfg)
check("deleting it afterwards sticks", "explanation" in keys(mq_of(cfg)), False)

kg = next(t for t in cfg["tools"]["knowledge_gaps"]["types"] if t["key"] == "kg")
check("other types untouched", keys(kg), ["notes"])

fresh = c._deep_merge(c.DEFAULTS, {})
c._migrate_mq_explanation_field(fresh)
check("a fresh install has it once", keys(mq_of(fresh)).count("explanation"), 1)

print()
print("FAILED: " + ", ".join(fails) if fails else "all green")
sys.exit(1 if fails else 0)
