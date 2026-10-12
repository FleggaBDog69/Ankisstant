"""Claude Code PostToolUse hook: after an edit to a .py file in this add-on,
run scratchpad/run_all.py and hand any failure straight back to Claude.

Wired in .claude/settings.json. Reads the hook payload on stdin. Exit 0 =
nothing to say; exit 2 with the failure on stderr = Claude sees it and fixes
it before moving on."""
import json, os, subprocess, sys

ROOT = os.path.realpath(os.path.join(os.path.dirname(__file__), "..", ".."))

try:
    payload = json.load(sys.stdin)
except Exception:
    sys.exit(0)
inp = payload.get("tool_input") or {}
resp = payload.get("tool_response") or {}
path = inp.get("file_path") or resp.get("filePath") or ""
if not path.endswith(".py"):
    sys.exit(0)
# realpath both sides: the add-on is also reachable through the
# ~/Documents/Projects/Ankisstant symlink.
real = os.path.realpath(path)
if not real.startswith(ROOT + os.sep):
    sys.exit(0)

r = subprocess.run([sys.executable, os.path.join(ROOT, "scratchpad", "run_all.py")],
                   cwd=ROOT, capture_output=True, text=True, timeout=110)
if r.returncode == 0:
    sys.exit(0)
out = (r.stdout + r.stderr).strip()
print(f"scratchpad/run_all.py failed after editing {os.path.relpath(real, ROOT)}:\n"
      + out[-3000:], file=sys.stderr)
sys.exit(2)
