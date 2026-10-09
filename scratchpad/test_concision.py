"""Short, atomic cards by default (2026-10-10).

Pins the pieces that keep AI-written cloze cards AnKing-length:
  - the visible-word counter and the >25-word prefilter (D4)
  - the length limit is NOT applied to cards already in the deck
  - the tighten pass (over-length + story-card allowance), with a stubbed AI
  - the grader always carries the full inline rubric; no dead CLI skill
  - single-fact points: the raised point ranges and the one-card-per-point floor
  - the work-through style examples reach the generation prompt
"""
import harness  # noqa: F401  — the package shell and the aqt/anki stubs
import importlib
import sys
import types

ADDON = harness.ADDON
FAILS = []


def check(name, cond):
    print(("PASS  " if cond else "FAIL  ") + name)
    if not cond:
        FAILS.append(name)


def _stub(name, **attrs):
    m = sys.modules.get(name) or harness.shell(name)
    m.__getattr__ = lambda n: (lambda *a, **k: None) if not n.startswith("__") else (_ for _ in ()).throw(AttributeError(n))
    for k, v in attrs.items():
        setattr(m, k, v)
    return m


# card_creator imports a lot of the add-on; stub what isn't under test.
anki_utils = importlib.import_module("ank.core.anki_utils")
sys.modules["ank.core"].anki_utils = anki_utils
CFG = {"provider": "cli"}
_stub("ank.core.config", tool_config=lambda t: {}, load_config=lambda: CFG,
      save_tool_config=lambda *a, **k: None, auto_tag_base=lambda: "!!Fleg",
      content_segment=lambda: "KG", month_tag=lambda *a, **k: "")
_stub("ank.core.qt_utils", is_manual_provider=lambda: False)
_stub("ank.core.synapse")
harness.shell("ank.grounding", ADDON + "/grounding")
_stub("ank.grounding.guidelines")
for mod in ("autotag", "pdf_render", "commit"):
    _stub("ank.tools." + mod)
harness.shell("ank.tools.kg", ADDON + "/tools/kg")
gui = sys.modules["aqt"]
gui.gui_hooks = types.SimpleNamespace()
sys.modules["aqt"].webview = _stub("aqt.webview")

qp = importlib.import_module("ank.tools.quality_pass")
cc = importlib.import_module("ank.tools.card_creator")
triage = importlib.import_module("ank.tools.kg.triage")
gen_prompt = importlib.import_module("ank.tools.kg.gen_prompt")
pipeline = importlib.import_module("ank.tools.pipeline")


# ── the counter ──────────────────────────────────────────────────────────────
check("one constant, two modules", qp.MAX_FRONT_WORDS == cc.MAX_FRONT_WORDS == 25)
check("cloze answers count, markup doesn't",
      qp.front_word_count("<b>Kawasaki disease</b> can cause coronary artery {{c1::aneurysms}}.") == 7)
check("hints don't count",
      qp.front_word_count("Clomiphene {{c1::increases::increases/decreases}} FSH") == 3)
check("entities and dashes aren't words",
      qp.front_word_count("A&nbsp;B — C &amp; D") == 4)
check("multi-word answer counts every word",
      qp.front_word_count("{{c1::transthoracic echocardiography}} first") == 3)

LONG = ("In newly diagnosed <b>heart failure</b>, {{c1::transthoracic echocardiography}} "
        "is the <u>initial</u> test — it quantifies ventricular function and detects "
        "stenotic or regurgitant lesions that may be a surgically correctable cause.")
SHORT = "The <u>initial</u> test in newly diagnosed <b>heart failure</b> is {{c1::transthoracic echocardiography}}."
check("the audit's long card is over the limit", qp.front_word_count(LONG) > 25)

# ── the prefilter ────────────────────────────────────────────────────────────
v = qp.prefilter_card({"front": LONG}, 2)
check("over 25 words fails D4", v is not None and v.verdict == qp.FAIL and v.worst_dim == "D4")
check("its reason names the count", v is not None and "words" in v.reason)
check("a short card passes the prefilter", qp.prefilter_card({"front": SHORT}, 2) is None)
check("max_words=0 switches the length check off",
      qp.prefilter_card({"front": LONG}, 2, max_words=0) is None)
check("answer-in-prompt still beats length",
      qp.prefilter_card({"front": "Spasm " * 30 + "{{c1::spasm}}"}, 2).worst_dim == "D1")
check("found deck cards aren't length-flagged", pipeline.check_found(LONG, 2) is None)
merged = qp.apply_prefilters_then([qp.Verdict(qp.PASS)], [{"front": LONG}], 2)
check("a lenient LLM pass can't wave a long card through", merged[0].verdict == qp.FAIL)

# ── the rubric ───────────────────────────────────────────────────────────────
check("rubric has a concision dimension", "D4 — Concision" in qp.RUBRIC_TEXT)
check("rubric no longer fails direct cues", "label/category is restated" not in qp.RUBRIC_TEXT
      and "direct" in qp.RUBRIC_TEXT.lower())
check("rubric max is 24", "Weighted max = 24" in qp.RUBRIC_TEXT)
check("skill grader prompt carries the full rubric",
      qp.RUBRIC_TEXT in qp.skill_batch_system(2))
CFG["provider"] = "cli"
check("no dead skill invocation on the CLI grader",
      cc._grader_skill({"quality_pass": {"prefer_skill": True,
                                         "grader_skill_invocation": "Use the anki-card-scorer skill"}}, None) == ("", ""))
CFG["provider"] = "anthropic"
check("an uploaded API skill id still rides along",
      cc._grader_skill({"quality_pass": {"grader_skill_id": "skill_x"}}, None) == ("", "skill_x"))
CFG["provider"] = "cli"

# ── the writing prompt ───────────────────────────────────────────────────────
check("the label-cue ban is gone", "LABEL CUE (BANNED)" not in cc.CARD_GEN_SYSTEM)
check("the 25-word limit is stated", "at most 25 words" in cc.CARD_GEN_SYSTEM)
check("story cards are rationed", "ONE IN FIVE" in cc.CARD_GEN_SYSTEM)
check("each card reports its style", '"style": "fact" | "story"' in cc.CARD_GEN_SYSTEM)
check("split prompt carries the limit", "25 words" in cc.SPLIT_SYSTEM)
check("depth means more cards, not longer",
      "never longer" in cc._breadth_directive(12, "topic", band="mechanism"))

# ── tighten ──────────────────────────────────────────────────────────────────
check("story allowance is one in five", [cc.story_allowance(n) for n in (1, 4, 5, 9, 10)] == [0, 0, 1, 1, 2])
cards = [{"front": SHORT, "extra": "", "style": "fact"},
         {"front": LONG, "extra": "x", "style": "fact", "point": 3, "slide": 2},
         {"front": "A man collapses {{c1::x}}", "extra": "", "style": "story"},
         {"front": "A woman faints {{c1::y}} after a sting and more words here", "extra": "", "style": "story"}]
todo = cc.cards_needing_tighten(cards)
check("over-length card is picked", 1 in todo and "TOO LONG" in todo[1])
check("4 cards allow no story — both stories picked", 2 in todo and 3 in todo)
check("a short fact card is left alone", 0 not in todo)

calls = []


def fake_ai(**k):
    calls.append(k)
    return [{"index": 1, "cards": [{"front": SHORT, "extra": ""},
                                   {"front": "Echo shows {{c1::valve}} lesions.", "extra": ""}]},
            {"index": 2, "cards": [{"front": "Short {{c1::x}}", "extra": ""}]},
            {"index": 3, "cards": []},          # dropped → original kept
            {"index": 99, "cards": [{"front": "stray"}]}]


cc.core_api.ask_claude_json = fake_ai
out, changed = cc.tighten_cards(cards, {"name": "AnKing"}, "m")
check("tighten made one call", len(calls) == 1 and changed)
check("a two-fact card became two cards", len(out) == 5)
check("point and slide carried onto both halves",
      out[1].get("point") == 3 and out[2].get("point") == 3 and out[2].get("slide") == 2)
check("rewritten cards are marked fact", out[1]["style"] == "fact" and out[3]["style"] == "fact")
check("a card the model dropped stays as it was", out[4]["front"].startswith("A woman"))
check("a stray index is ignored", not any(c["front"] == "stray" for c in out))
calls.clear()
out, changed = cc.tighten_cards(cards, {"card_format": "qa"}, "m")
check("Q&A profile is never tightened", not changed and not calls)
out, changed = cc.tighten_cards([{"front": SHORT, "extra": ""}], None, "m")
check("nothing to do makes no call", not changed and not calls)
cc.core_api.ask_claude_json = lambda **k: None
out, changed = cc.tighten_cards(cards, None, "m")
check("a failed call leaves the cards alone", not changed and out is cards)

# ── points ───────────────────────────────────────────────────────────────────
check("search points raised", triage.SCOPES["focused"]["points"] == (2, 5)
      and triage.SCOPES["broad"]["points"] == (6, 12))
check("create points raised", triage.CREATE_BREADTHS["core"]["points"] == (3, 6)
      and triage.CREATE_BREADTHS["wide"]["points"] == (8, 14)
      and triage.CREATE_BREADTHS["deep"]["points"] == (12, 20))
check("both decompositions carry the one-fact rule",
      triage._ONE_FACT_POINT in triage._S_DECOMPOSE and triage._ONE_FACT_POINT in triage._C_DECOMPOSE)
check("no compound example point left", "additionally blocks" not in triage._DECOMP_CREATE_SYSTEM
      and "additionally blocks" not in triage._CREATE_SYSTEM)
obj = {"breadth": "wide", "cards": 8,
       "points": [{"concept": f"fact {i}", "terms": ["a b"]} for i in range(12)]}
plan = triage._plan_from_obj(obj, {"id": "k1", "title": "t"}, flow="create", require_terms=False)
check("budget floored at one card per point", plan.cards == 12)
plan = triage._plan_from_obj(obj, {"id": "k1", "title": "t"}, flow="create",
                             require_terms=False, lock={"cards": 5})
check("a budget the user set is left alone", plan.cards == 5)
plan = triage._plan_from_obj(obj, {"id": "k1", "title": "t"}, flow="search")
check("search budgets are not floored", plan.cards == 8)

# ── style examples ───────────────────────────────────────────────────────────
blk = gen_prompt._style_block(["A {{c1::b}}", "", "C {{c1::d}}"])
check("style block lists the examples", "- A {{c1::b}}" in blk and "- C {{c1::d}}" in blk)
check("style block says not to repeat them", "Do NOT repeat" in blk)
check("no examples, no block", gen_prompt._style_block([]) == "")

print("\nall green" if not FAILS else f"\n{len(FAILS)} FAILED")
sys.exit(1 if FAILS else 0)
