"""Run the lint and every offline test: python3 scratchpad/run_all.py

Each test_*.py runs in its own interpreter, so one script's module shells
can't leak into the next. Exits non-zero if any script fails."""
import pathlib, subprocess, sys

here = pathlib.Path(__file__).parent
failed = []
# Lint first: an undefined name breaks a button press long before a test
# would notice. Skips (and passes) if setup_dev.sh hasn't been run.
for test in [here / "tools" / "lint.py", *sorted(here.glob("test_*.py"))]:
    r = subprocess.run([sys.executable, str(test)], capture_output=True, text=True, cwd=here)
    ok = r.returncode == 0
    print(("ok    " if ok else "FAIL  ") + test.name)
    if not ok:
        failed.append(test.name)
        print((r.stdout + r.stderr).strip()[-2000:])
print()
print(f"{len(failed)} of the test scripts failed: {', '.join(failed)}" if failed else "all green")
sys.exit(1 if failed else 0)
