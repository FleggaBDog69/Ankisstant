import harness_tag as h
at = h.autotag

LV = {"system": "Neurology", "subsystem": "Trauma", "topic": "mTBI"}
fails = []
def check(name, got, want):
    ok = got == want
    print(("PASS  " if ok else "FAIL  ") + name)
    if not ok:
        print(f"        got  {got!r}\n        want {want!r}")
        fails.append(name)

# ── the source axis ──────────────────────────────────────────────────────────
print("-- source --")
check("a course group with a blank source gets NO source",
      at.source_path_for(tool="knowledge_gaps", group="Concussion CIW"), "")
check("a group that HAS a source still gets it",
      at.source_path_for(tool="knowledge_gaps", group="GP Day 1"),
      "Placements::GP")
check("the sticky is still there for a door that asks for it by tool",
      at.source_path_for(tool="knowledge_gaps"), "Murtaghs::other")

# THE WORK-THROUGH NO LONGER ASKS. Its Source box is gone, so bulk_search
# ._source_path() calls source_path_for WITHOUT `tool` — which is the only way
# to guarantee the remembered path can never come back. These four are the
# whole of the Murtagh's bug: the stored value is still Murtaghs::other (above)
# and none of these may return it.
print("-- the work-through's own derivation (no Source box) --")
check("an ungrouped work-through gets NO source, sticky or not",
      at.source_path_for(kg_type_key="", group=""), "")
check("a course group with a blank source still gets none",
      at.source_path_for(kg_type_key="", group="Concussion CIW"), "")
check("a group that HAS a source still supplies it",
      at.source_path_for(kg_type_key="", group="GP Day 1"), "Placements::GP")
check("an ungrouped MQ gap still says MQ",
      at.source_path_for(kg_type_key="mq", group=""), "MQ")
check("an explicit empty override still means no source",
      at.source_path_for(tool="knowledge_gaps", group="GP Day 1", override=""), "")
check("a lecture file still wins",
      at.source_path_for(tool="knowledge_gaps", group="Concussion CIW",
                         lecture_path="/x/cardiac_output.pdf"),
      "Lecture::cardiac_output")
check("an MQ gap in a sourceless group still says MQ",
      at.source_path_for(tool="knowledge_gaps", group="Concussion CIW",
                         kg_type_key="mq"), "MQ")
check("the source TAG for the concussion group is empty",
      at.source_tag_for(tool="knowledge_gaps", group="Concussion CIW"), "")

# ── the content / course axis ────────────────────────────────────────────────
print("-- tags --")
check("a course group's branch IS the classification tag",
      at.content_tag_from_levels(LV, group="Concussion CIW"),
      "!!Curtanki::Year_3::CIW::Concussion::mTBI")
check("a course group's card carries ONE tag, not two",
      at.tags_for(LV, group="Concussion CIW", tool="knowledge_gaps"),
      ["!!Curtanki::Year_3::CIW::Concussion::mTBI"])
check("...and the same for SRS 19",
      at.tags_for(LV, group="SRS 19 HR Preg", tool="knowledge_gaps"),
      ["!!Curtanki::Year_3::SRS::19::KGs::mTBI"])
print("      GP Day 1 (relative, source set):",
      at.tags_for(LV, group="GP Day 1", tool="knowledge_gaps"))
print("      no group at all:               ",
      at.tags_for(LV, tool="knowledge_gaps"))
print("      an ordinary new group:         ",
      at.tags_for(LV, group="Antiepileptics", tool="knowledge_gaps"))

# A course branch on a group told NOT to tag as one applies no branch, so the
# content tag must stay — otherwise the card is classified nowhere.
plan = at.group_tag_plan("Concussion CIW", tag_as_group=False)
check("a group told not to tag as one keeps its content tag",
      at.content_tag_from_levels(LV, group="Concussion CIW", group_plan=plan),
      "!!Fleg::KG::Neurology::Trauma::mTBI")
# An unclassified gap in a course group gets the branch HEAD — which is the
# tag its cards carried anyway (the group branch was applied beside the content
# tag), and is now what `auto_tag` caches, so Retag group can still find them.
check("an unclassified gap in a course group gets the branch head",
      at.content_tag_from_levels({}, group="Concussion CIW"),
      "!!Curtanki::Year_3::CIW::Concussion")
check("an unclassified gap with no group still yields nothing",
      at.content_tag_from_levels({}), "")

print()
print("FAILED: " + ", ".join(fails) if fails else "all green")
