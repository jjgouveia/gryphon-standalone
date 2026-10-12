## Summary

`query_graph_tool callers_of` now lists every call site, not one per calling function.

An impact benchmark (below) found that a model using the graph to find "every call that must change when this signature changes" lost recall on the targets where a function calls the target more than once. The graph stores every call (one edge per call site); the response collapsed them into one result per caller with no line number, and the minimal view showed only five callers.

- **`call_lines` and `call_count` per caller.** Every line the caller calls the target on (up to 25 are listed, `call_count` is the total). Both views carry them.
- **Minimal view shows up to 25 callers for `callers_of`** (other patterns keep five). A minimal entry is a name, a file and a few line numbers.
- **Name-only matches are marked in the minimal view too** (`target_resolution: "unresolved"`, which the standard view already had). The bare name may be another function that shares it, and the minimal view hid that. Lines of a caller already matched exactly are not mixed with its bare-name lines.
- **An omission says how to get the rest** (`for all N use detail_level="standard" with max_results=N`, or `pass max_results=N for all`).

## Evidence

The benchmark asks an agent to list every call of a function whose signature is about to change, scored against the TypeScript checker's own resolution, in a 5.7k-file TypeScript/React app. Targets are the functions where a plain grep is noisiest (up to 479 grep lines for 11 real calls, names declared in up to 13 places) plus four easy ones. Haiku 4.5 reviewing, one run per target and arm.

`callers_of` alone, line-level recall against the checker's call sites (14 targets, 171 calls):

| view | hard targets (123 calls) | easy targets (48 calls) |
|---|---|---|
| before (one line per caller) | 63% | 73% |
| minimal, new defaults | 88% | 100% |
| standard | **100%** | **100%** |

An agent using the tool (Haiku 4.5, graph arm against a grep-only arm):

| | before | after |
|---|---|---|
| hard targets: recall | 0.951 (grep: 1.000) | **1.000** (grep: 1.000) |
| hard targets: precision | 0.936 | 0.969 |
| hard targets: cost per run | US$ 0.095 (grep: 0.123) | US$ 0.079 (grep: 0.123) |
| easy targets: cost per run | US$ 0.077 (grep: 0.047) | US$ 0.041 (grep: 0.056) |

Paired by target on the hard set, the graph arm now costs −US$ 0.044 per run [−0.093; −0.002] and holds the recall; before it lost 4.9 points of recall. One run per target and arm, 14 targets: read the intervals, not the point values.

For scale: with Sonnet 5.5 the graph made no difference in this benchmark (a grep-only agent filtered the noise itself). The change matters for weaker models, which trust the first list the tool returns.

## Test plan

- [x] New tests (`TestCallersOfCallLines`): every line per caller in both views; the 25-line cap with the true count; name-only callers marked in the minimal view; bare-name lines not leaking into an exactly resolved caller; the minimal cap of 25 and the omission hints.
- [x] Written with POSIX paths, so they also run on Windows (the existing helper in that module builds backslash paths and its tests fail there on a clean `staging`).
- [x] `ruff` clean. `test_tools`, `test_agent_transparency`, `test_pr853_edges`, `test_token_budget`, `test_main` and the spring suites: the same failures as a clean `staging` on Windows, none new.
- [ ] CI on Linux.

🤖 Generated with [Claude Code](https://claude.com/claude-code)
