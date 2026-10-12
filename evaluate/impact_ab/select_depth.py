"""Pick transitive-impact targets from callgraph.json by a fixed rule.

For a function X: level 1 = the functions that call it (or render it as a JSX component),
level 2 = the functions that call a level-1 function. A target qualifies when its closure
is large enough to need many lookups and contains no top-level module code.
"""
import json
import random
from collections import defaultdict
from pathlib import Path

here = Path(__file__).parent
g = json.loads((here / "callgraph.json").read_text(encoding="utf-8"))
nodes, edges = g["nodes"], g["edges"]
callers = defaultdict(set)
for caller, callee, _file, _line in edges:
    if caller != callee:
        callers[callee].add(caller)

FRAMEWORK = {"GET", "POST", "PUT", "PATCH", "DELETE", "HEAD", "OPTIONS", "proxy", "middleware", "default"}


def closure(key):
    d1 = callers[key] - {key}
    d2 = set()
    for k in d1:
        d2 |= callers[k]
    d2 -= d1 | {key}
    return d1, d2


def directory(key):
    return str(Path(nodes[key]["file"]).parent)


eligible = []
for key, n in nodes.items():
    if not n["exported"] or n["kind"] != "function" or n["name"] in FRAMEWORK:
        continue
    if n["file"].endswith(("/route.ts", "/route.tsx", "/page.tsx", "/layout.tsx")):
        continue
    d1, d2 = closure(key)
    members = d1 | d2
    if any(m.startswith("<module>") for m in members):
        continue
    files = {nodes[m]["file"] for m in members}
    if 3 <= len(d1) <= 10 and 6 <= len(d2) <= 40 and len(files) >= 5:
        eligible.append(key)
eligible.sort()
random.Random(5).shuffle(eligible)

picked, dirs = [], set()
for key in eligible:
    if directory(key) in dirs:
        continue
    dirs.add(directory(key))
    picked.append(key)
    if len(picked) == 8:
        break

targets = []
for i, key in enumerate(picked):
    n = nodes[key]
    d1, d2 = closure(key)

    def info(k):
        m = nodes[k]
        return {"key": k, "name": m["name"], "file": m["file"], "line": m["line"]}

    targets.append({
        "id": f"d{i + 1:02d}-{n['name']}", "name": n["name"], "file": n["file"], "line": n["line"],
        "group": "transitive", "level1": [info(k) for k in sorted(d1)], "level2": [info(k) for k in sorted(d2)],
    })
(here / "targets_depth.json").write_text(json.dumps(targets, indent=1), encoding="utf-8")
print(f"{len(eligible)} eligible; picked {len(targets)}")
for t in targets:
    files = {m["file"] for m in t["level1"] + t["level2"]}
    print(f"{t['id']:40} level1={len(t['level1']):2} level2={len(t['level2']):2} files={len(files):2}  {t['file']}")
