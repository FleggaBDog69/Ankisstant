"""cut.py FILE NAME [NAME...] — delete top-level or class-level defs/classes
by name (Class.method or name), including their decorators and the comment
block directly above them. Prints what went."""
import ast, sys
path, names = sys.argv[1], set(sys.argv[2:])
src = open(path).read()
lines = src.splitlines(keepends=True)
tree = ast.parse(src)
spans = []
def visit(body, prefix=""):
    for node in body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            full = prefix + node.name
            if full in names or (not prefix and node.name in names):
                start = min([node.lineno] + [d.lineno for d in node.decorator_list]) - 1
                # swallow the comment block directly above
                while start > 0 and lines[start-1].strip().startswith("#"):
                    start -= 1
                end = node.end_lineno
                # and the blank lines after
                while end < len(lines) and not lines[end].strip():
                    end += 1
                spans.append((start, end, full))
                names.discard(full); names.discard(node.name)
            elif isinstance(node, ast.ClassDef):
                visit(node.body, node.name + ".")
visit(tree.body)
for start, end, full in sorted(spans, reverse=True):
    print(f"cut {full}: lines {start+1}-{end} ({end-start})")
    del lines[start:end]
open(path, "w").write("".join(lines))
if names:
    print("NOT FOUND:", sorted(names)); sys.exit(1)
