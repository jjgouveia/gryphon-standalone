---
name: review-delta
description: Review only changes since last commit using impact analysis. Token-efficient delta review with automatic blast-radius detection; a quick look, not a merge gate. Escalate to review-multi or the review-pr lanes when the change decides a merge.
argument-hint: "[file or function name]"
---

# Review Delta

Review only the changed code and its blast radius.

This is the cheap path: the graph pass and the source read happen in one
context when the graph is current. Without a current graph, it is a source-only
quick look; that is still enough to start. It is a quick look, not a merge
gate. A single pass that has already read the graph's risk list will look for
confirmation in the diff; that is acceptable for a cheap look and not
acceptable for a decision to merge.

## Steps

1. Call `get_minimal_context_tool(task="review changes")` once.
   - If the graph is usable (built on the current branch or its base, and it
     covers the changed files), use it for the steps below.
   - If it is `stale_graph`, `missing_graph`, or built on an unrelated branch,
     **do not build it before the first pass**. Continue from
     `git diff HEAD~1` and the source, and record `graph: not used`. A rebuild
     is a separate operation; run it only if the user asked for graph
     maintenance or the quick look escalates to a review that needs it.
2. Only when the graph is usable, call
   `detect_changes_tool(detail_level="minimal")` for risk-scored changed
   functions, test gaps and affected flows. Changes come from `git diff`
   against `HEAD~1`; if the argument names a file, pass it in `changed_files`.
3. When the graph is usable, call `get_review_context_tool(detail_level="minimal")` when you need source snippets for the changed areas and `review_guidance` (untested functions, wide blast radius, inheritance changes).
4. For each untested high-risk function, confirm with `query_graph_tool(pattern="tests_for", target="<function>")`.
5. Use `detail_level="standard"` only for a high-risk item the minimal output leaves unclear. Do not load whole files unless a snippet is not enough.

## When it is a gate

Escalate before writing a merge recommendation when any of these is true:

- risk is high, or the diff touches a path from `review-pr`'s *Paths that
  earn a deep read*;
- the user asked for a merge decision, not a quick look;
- the change has more than one commit or changes a public contract.

Escalating means `review-multi` (two blind generalists) or the lane protocol
in `review-pr` (correctness, tests, blast radius), not a second pass in this
context. If you do run a second pass, give it the packet, not the findings
you already have; the second pass must not see this session's conclusions.

If no subagent tool is available, say `single-context`. A gate from one
context is a pre-check, not a GO.

## Report

- Summary: one line
- Risk: low, medium or high, from the blast radius
- Issues: bugs, missing tests, style
- Blast radius: impacted files and functions
- Independence: lanes run, or `single-context`
- Recommendations
