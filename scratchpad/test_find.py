"""The mTBI run, as a fixture. Numbers taken from his own trace log
(user_files/ankisstant.log, 2026-09-21 13:29:57)."""
import harness
m = harness.matcher
LP = m.LearningPoint

LOC   = 1537485987706   # "...football player... who was wobbly but DID NOT have LOC"
AMN   = 1530553281415   # "...LOC and retrograde amnesia following head injury"
CT    = 1754465540140   # "...Canadian CT Head Rules in a patient post-TBI with GCS 13-15"

def mk(concept, terms, cands, scores, slots=None, kg="4654a3295160"):
    pm = m.PointMatch(point=LP(concept=concept, topic="mTBI", system="Neurology",
                              terms=terms, kg_id=kg))
    pm.nids = list(cands)
    # `kept` exactly as parse_scoring_reply now builds it: best-first, and
    # against the GAP floor for an item that came from a knowledge gap.
    floor = m._GAP_MIN_SCORE if kg else m._KEEP_MIN_SCORE
    pm.kept = [n for n in sorted(cands, key=lambda x: -scores[x])
               if scores[n] >= floor]
    pm.scores = dict(scores)
    pm.judge_slot = dict(slots or {})
    pm.term_cover = {n: frozenset({0}) for n in cands}
    pm.susp = {n: (1, 1) for n in cands}
    pm.scored = True
    return pm

fails = []
def check(name, got, want):
    ok = got == want
    print(("PASS  " if ok else "FAIL  ") + name)
    if not ok:
        print(f"        got  {got}\n        want {want}")
        fails.append(name)

# ── 1. ownership: the point that scored the card 3 keeps it ──────────────────
p_loc = mk("mTBI involves loss of consciousness of less than 30 minutes",
           ["concussion consciousness", "mTBI LOC"],
           [AMN, LOC], {AMN: 3, LOC: 2}, {AMN: 0, LOC: 0})
p_amn = mk("mTBI involves post-traumatic amnesia of less than 24 hours",
           ["mTBI amnesia", "concussion amnesia"],
           [AMN], {AMN: 2}, {AMN: 1})
# cap_for=1: "narrow" scope, one assertion, one card — what triage hands the
# allocator for a gap like this (tools/kg/triage.SCOPES).
m.allocate([p_loc, p_amn], budget=4, cap_for=lambda _m: 1)
check("the 3-scored card goes to the point that scored it 3",
      p_loc.selected, [AMN])
check("the neighbour that scored it 2 does not take it", p_amn.selected, [])
check("the neighbour reads as an honest gap instead", p_amn.is_gap, True)
check("the LOC gap is NOT shown the DID-NOT-lose-consciousness card",
      LOC in p_loc.selected, False)

# ── 2. a 2 no longer covers a gap ────────────────────────────────────────────
gap = LP(concept="mTBI is typically defined by a GCS of 13-15",
         terms=["mTBI GCS"], kg_id="d17b157fe463")
lec = LP(concept="Amphotericin B causes nephrotoxicity", terms=["amphotericin"])
batch = [(gap, [(CT, "Canadian CT Head Rules ... GCS 13-15")]),
         (lec, [(999, "Which ions are supplemented with amphotericin?")])]
v = m.parse_scoring_reply({"1": {str(CT): 2}, "2": {"999": 2}}, batch)
check("a score-2 card does not cover a GAP", v[0].kept, [])
check("a score-2 card still covers a lecture POINT", v[1].kept, [999])
v3 = m.parse_scoring_reply({"1": {str(CT): 3}, "2": {"999": 3}}, batch)
check("a score-3 card does cover a gap", v3[0].kept, [CT])

# ── 3. a judge FAILURE must still fail open, gap or not ──────────────────────
v = m.parse_scoring_reply("not json at all", batch)
check("unparseable reply keeps the gap's candidates", v[0].kept, [CT])
check("unparseable reply is flagged unfiltered", v[0].filtered, False)
v = m.parse_scoring_reply({"2": {"999": 3}}, batch)
check("a gap omitted from the reply keeps its candidates", v[0].kept, [CT])
v = m.parse_scoring_reply({"1": {}, "2": {"999": 3}}, batch)
check("a gap whose id the model skipped keeps it (default 2 bypasses the floor)",
      v[0].kept, [CT])

# ── 4. a user-driven re-search stays lenient ─────────────────────────────────
v = m.parse_scoring_reply({"1": {str(CT): 1}, "2": {"999": 1}}, batch,
                          min_score=m.RESEARCH_MIN_SCORE)
check("re-search still offers a score-1 card on a gap", v[0].kept, [CT])

# ── 5. the prompt tells the judge which it is ────────────────────────────────
prompt = m.build_scoring_prompt(batch)
check("a KG item is headed GAP", "GAP 1:" in prompt, True)
check("a lecture item is still headed POINT", "POINT 2:" in prompt, True)
check("the rubric carries the gap rules",
      "AN ITEM HEADED 'GAP' IS NOT A LECTURE HEADING" in m.RELEVANCE_SYSTEM, True)

print()
print("FAILED: " + ", ".join(fails) if fails else "all green")
