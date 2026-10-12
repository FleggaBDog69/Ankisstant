"""Every `from .x import y` inside the add-on names something that exists.

pyflakes can't see this: an import of a deleted function is syntactically
fine and only fails when Anki loads the add-on — which takes the whole
add-on down, not just the feature. Checked statically, so no Anki needed."""
import ast, pathlib, sys

ROOT = pathlib.Path(__file__).resolve().parent.parent
SKIP = ("scratchpad", "_phaseA", "user_files")

def module_path(src: pathlib.Path, level: int, mod: str | None):
    base = src.parent
    for _ in range(level - 1):
        base = base.parent
    p = base.joinpath(*(mod.split(".") if mod else []))
    if p.with_suffix(".py").exists():
        return p.with_suffix(".py")
    if (p / "__init__.py").exists():
        return p / "__init__.py"
    return None

def defined(path: pathlib.Path) -> set:
    t = ast.parse(path.read_text())
    names = set()
    for n in ast.walk(t):
        if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)) and n in t.body:
            names.add(n.name)
    for n in t.body:
        if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            names.add(n.name)
        elif isinstance(n, (ast.Assign, ast.AnnAssign)):
            targets = n.targets if isinstance(n, ast.Assign) else [n.target]
            for tg in targets:
                for x in ast.walk(tg):
                    if isinstance(x, ast.Name):
                        names.add(x.id)
        elif isinstance(n, (ast.Import, ast.ImportFrom)):
            for a in n.names:
                names.add((a.asname or a.name).split(".")[0])
        elif isinstance(n, (ast.If, ast.Try)):
            for x in ast.walk(n):
                if isinstance(x, (ast.FunctionDef, ast.ClassDef)):
                    names.add(x.name)
                elif isinstance(x, ast.Name) and isinstance(x.ctx, ast.Store):
                    names.add(x.id)
                elif isinstance(x, (ast.Import, ast.ImportFrom)):
                    for a in x.names:
                        names.add((a.asname or a.name).split(".")[0])
    return names

bad = []
for src in ROOT.rglob("*.py"):
    if any(s in str(src) for s in SKIP):
        continue
    for n in ast.walk(ast.parse(src.read_text())):
        if isinstance(n, ast.ImportFrom) and n.level > 0:
            target = module_path(src, n.level, n.module)
            if target is None:
                # `from . import x` where x is a submodule
                for a in n.names:
                    if module_path(src, n.level, ((n.module + ".") if n.module else "") + a.name) is None:
                        bad.append(f"{src.relative_to(ROOT)}:{n.lineno}: no module for {n.module}.{a.name}")
                continue
            have = defined(target)
            for a in n.names:
                if a.name == "*" or a.name in have:
                    continue
                sub = ((n.module + ".") if n.module else "") + a.name
                if module_path(src, n.level, sub) is not None:
                    continue
                bad.append(f"{src.relative_to(ROOT)}:{n.lineno}: {a.name} is not in {target.relative_to(ROOT)}")
for b in bad:
    print("FAIL  " + b)
if bad:
    print(f"{len(bad)} broken import(s)")
    sys.exit(1)


# ── module.attr: `from .. import card_creator as cc` then `cc.gone()` ──────────
# The same failure, one step later: it only breaks when that line runs.
attr_bad = []
for src in ROOT.rglob("*.py"):
    if any(s in str(src) for s in SKIP):
        continue
    tree = ast.parse(src.read_text())
    aliases = {}
    for n in ast.walk(tree):
        if isinstance(n, ast.ImportFrom) and n.level > 0:
            for a in n.names:
                sub = ((n.module + ".") if n.module else "") + a.name
                p = module_path(src, n.level, sub)
                if p is not None:
                    aliases[a.asname or a.name] = p
    if not aliases:
        continue
    cache = {}
    for n in ast.walk(tree):
        if (isinstance(n, ast.Attribute) and isinstance(n.value, ast.Name)
                and n.value.id in aliases and isinstance(n.ctx, ast.Load)):
            p = aliases[n.value.id]
            have = cache.setdefault(p, defined(p))
            if n.attr not in have and module_path(p, 1, n.attr) is None \
                    and not n.attr.startswith("__"):
                attr_bad.append(f"{src.relative_to(ROOT)}:{n.lineno}: "
                                f"{n.value.id}.{n.attr} is not in {p.relative_to(ROOT)}")
for b in sorted(set(attr_bad)):
    print("FAIL  " + b)
if attr_bad:
    sys.exit(1)

print("all green")
