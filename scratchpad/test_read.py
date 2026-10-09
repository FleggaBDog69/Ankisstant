"""What the model is actually shown for a gap — the notes half of the prompt.

The bug this pins: "other groin lumps" carried 1,282 characters of headed
differential and the prompt carried 300 of them, flattened onto one line and
cut mid-word inside "saphenofemoral". The reading that came back —
"lymph node causes (reactive, lymphoma, metastatic) and saphena varix" — was a
faithful summary of the third it was given.
"""
import harness  # noqa: F401  — the package shell and the aqt/anki stubs
import sys, types, importlib

harness.shell("ank.tools.kg", harness.ADDON + "/tools/kg")
triage = importlib.import_module("ank.tools.kg.triage")

NOTES = """Lymph nodes

Reactive lymphadenopathy — infection draining the leg/perineum/genitals (cellulitis, STI, insect bite)
Lymphoma — firm, rubbery, non-tender, often multiple/matted
Metastatic nodes — from penile, vulval, anal, or lower limb melanoma/SCC

Vascular

Saphena varix — dilation of the saphenofemoral junction; soft, compressible, disappears on lying flat, may have a palpable cough impulse (mimics hernia), often with a bluish tinge and visible varicosities below it
Femoral artery aneurysm — pulsatile, expansile (not just transmitted pulsation)
Pseudoaneurysm — post-catheterisation, tender, pulsatile

Skin/soft tissue

Sebaceous cyst — punctum often visible, mobile over deep tissue, not reducible
Lipoma — soft, lobulated, non-tender, slow growing
Abscess — hot, tender, fluctuant, ± systemic signs; consider infected sebaceous cyst or hidradenitis suppurativa if recurrent

Undescended/ectopic testis

Palpable along the inguinal canal in a male with an empty ipsilateral scrotum — important not to miss, associated with malignancy risk

Muscle/tendon

Psoas abscess — presents low in the groin, associated with hip flexion (pain on extension), think TB or Crohn's-related in the right context
Iliopsoas bursitis — swelling anterior to the hip, worsens with hip flexion/rotation"""

fails = []
def check(name, ok, detail=""):
    print(("PASS  " if ok else "FAIL  ") + name)
    if not ok:
        if detail:
            print("        " + detail)
        fails.append(name)

block = triage._notes_for_prompt(NOTES)

print("-- the groin gap's notes, as the model now sees them --")
for term in ("Sebaceous cyst", "Undescended", "Psoas abscess",
             "Iliopsoas bursitis", "saphenofemoral"):
    check(f"'{term}' reaches the prompt", term in block)

check("nothing is cut — no truncation marker",
      "CONTINUE PAST HERE" not in block)
check("the headings survive as their own lines",
      "\n    Vascular\n" in block and "\n    Skin/soft tissue\n" in block,
      "headings were flattened into the prose again")
check("every note line is indented under its gap number",
      all(ln.startswith("    ") for ln in block.split("\n")[2:] if ln.strip()))

# The old behaviour, stated as a test so it cannot come back.
check("the old 300-char cut would have lost most of it",
      len(" ".join(NOTES.split())) > 1000)

# A pathological note is still capped, and says so.
huge = "\n".join(f"Point number {i} about something" for i in range(400))
cap = triage._notes_for_prompt(huge)
check("a runaway note is still capped", len(cap) < 2600, f"{len(cap)} chars")
check("...and the cut is announced, not silent",
      "CONTINUE PAST HERE" in cap)
check("...and it cuts at a line end, not mid-word",
      "Point number" in cap and "\n    Point numb\n" not in cap)

check("empty notes produce nothing at all", triage._notes_for_prompt("") == ""
      and triage._notes_for_prompt("   \n  ") == "")
check("HTML notes keep their line breaks",
      triage._notes_for_prompt("a<br>b<br>c").count("\n") >= 3)
check("HTML entities are decoded",
      "&amp;" not in triage._notes_for_prompt("salt &amp; water"))

print()
print("FAILED: " + ", ".join(fails) if fails else "all green")
