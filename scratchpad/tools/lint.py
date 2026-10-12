"""Lint the add-on for the mistakes that break loading or a button press.

    python3 scratchpad/tools/lint.py            # errors only (what run_all uses)
    python3 scratchpad/tools/lint.py --dead     # also list code nothing calls

Errors: undefined names (pyflakes). Imports of names that don't exist are
covered by scratchpad/test_imports.py. --dead runs vulture and drops the Qt
overrides it can't know Qt calls. Needs scratchpad/tools/setup_dev.sh once;
without the venv it says so and passes, so run_all still works anywhere."""
import pathlib, subprocess, sys

HERE = pathlib.Path(__file__).resolve().parent
ROOT = HERE.parent.parent
VENV = HERE.parent / ".venv" / "bin"
TARGETS = ["__init__.py", "core", "tools", "ui"]
QT_OVERRIDES = ("paintEvent", "createEditor", "setEditorData", "setModelData",
                "updateEditorGeometry", "contextMenuEvent", "createWindow",
                "sizeHint", "eventFilter", "closeEvent", "showEvent",
                "keyPressEvent", "resizeEvent", "splitPath")

def tool(name):
    p = VENV / name
    return str(p) if p.exists() else None

def main() -> int:
    pyflakes = tool("pyflakes")
    if pyflakes is None:
        print("lint skipped — run scratchpad/tools/setup_dev.sh once")
        return 0
    r = subprocess.run([pyflakes, *TARGETS], cwd=ROOT, capture_output=True, text=True)
    errors = [l for l in r.stdout.splitlines()
              if "undefined name" in l or "referenced before assignment" in l
              or "syntax error" in l.lower()]
    for l in errors:
        print("FAIL  " + l)
    if "--dead" in sys.argv:
        vulture = tool("vulture")
        r = subprocess.run([vulture, *TARGETS, "--min-confidence", "60"],
                           cwd=ROOT, capture_output=True, text=True)
        for l in r.stdout.splitlines():
            if "unused" in l and not any(q in l for q in QT_OVERRIDES) \
                    and ("function" in l or "method" in l or "class" in l):
                print("dead? " + l.replace(" (60% confidence)", ""))
    print("lint clean" if not errors else f"{len(errors)} lint error(s)")
    return 1 if errors else 0

if __name__ == "__main__":
    sys.exit(main())
