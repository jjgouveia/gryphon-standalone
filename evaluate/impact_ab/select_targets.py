"""Pick the benchmark targets from candidates.json by a fixed rule.

hard: exported functions where a grep for the name is noisy (F1 < 0.5), 6-40 call
      sites; the 10 with the most grep lines, at most 2 per name and 2 per directory.
easy: exported functions where grep is accurate (F1 >= 0.9), 10-30 call sites; 4,
      one per directory, chosen with a seeded shuffle.
"""
import json
import random
from collections import Counter
from pathlib import Path

here = Path(__file__).parent
cands = json.loads((here / "candidates.json").read_text(encoding="utf-8"))
# Framework entry points (Next.js route handlers and middleware) are called by the framework,
# not by code, and a grep for "GET" or "POST" matches the word everywhere.
FRAMEWORK_NAMES = {"GET", "POST", "PUT", "PATCH", "DELETE", "HEAD", "OPTIONS", "proxy", "middleware"}
ok = [
    d for d in cands
    if d["exported"] and d["kind"] == "function" and d["name"] not in FRAMEWORK_NAMES
    and not d["file"].endswith(("/route.ts", "/route.tsx"))
]


def directory(d):
    return str(Path(d["file"]).parent)


def pick(pool, n, per_name=2, per_dir=2):
    names, dirs, out = Counter(), Counter(), []
    for d in pool:
        if names[d["name"]] >= per_name or dirs[directory(d)] >= per_dir:
            continue
        out.append(d)
        names[d["name"]] += 1
        dirs[directory(d)] += 1
        if len(out) == n:
            break
    return out


hard_pool = sorted(
    (d for d in ok if 6 <= d["n_calls"] <= 40 and d["grep"]["f1"] < 0.5),
    key=lambda d: (-d["grep"]["lines"], d["file"], d["line"]),
)
easy_pool = sorted(
    (d for d in ok if 10 <= d["n_calls"] <= 30 and d["grep"]["f1"] >= 0.9),
    key=lambda d: (d["file"], d["line"]),
)
random.Random(7).shuffle(easy_pool)
targets = [{**d, "group": "hard"} for d in pick(hard_pool, 10)]
targets += [{**d, "group": "easy"} for d in pick(easy_pool, 4, per_name=1, per_dir=1)]
for i, t in enumerate(targets):
    t["id"] = f"t{i + 1:02d}-{t['name']}"
(here / "targets.json").write_text(json.dumps(targets, indent=1), encoding="utf-8")
print(f"{len(hard_pool)} hard and {len(easy_pool)} easy candidates")
for t in targets:
    g = t["grep"]
    print(f"{t['id']:34} {t['group']:5} calls={t['n_calls']:2} files={t['n_call_files']:2} "
          f"same-name={t['same_name_decls']:2} grep lines={g['lines']:4} P={g['precision']:.2f} R={g['recall']:.2f}")
