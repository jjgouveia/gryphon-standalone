---
name: review-changes
description: Perform a structured code review using change detection and impact. Pre-commit path; escalate to review-multi or the review-pr lanes for a merge gate.
---

## Review Changes

Review a change set with risk scores and blast radius from the knowledge graph
when it is current. The graph is optional context, not a gate: if it is missing
or stale, do not build it before the review. Start from the diff and source,
mark `graph: not used`, and say the risk/blast-radius output is source-derived.
A build or update is a separate, explicit task.

This is the pre-commit path, not a merge gate. Use `review-delta` for the
token-efficient look and `review-multi` or the `review-pr` lanes when the
change decides a merge. Do not chain a second review in this context: a
reviewer that has read the first list is checking the list, not the code.
If no subagent tool is available, say `single-context`.

### Steps

1. If the graph is usable, call `detect_changes_tool` for risk-scored changed functions, test gaps and affected flows. If not, do not call it (it may refresh the graph); work from `git diff` and the source.
2. Call `get_affected_flows_tool` only when the graph is usable and you need the steps of an affected flow.
3. For each high-risk function, call `query_graph_tool` with `pattern="tests_for"` to check test coverage.
4. Call `get_impact_radius_tool` when the graph is usable and the blast radius is not clear from step 1.
5. Suggest specific test cases for untested changes.

### Output Format

Group findings by risk level (high, medium, low). For each finding give what changed and why it matters, its test coverage, and the suggested fix. End with a merge recommendation, and say whether independent lanes ran or this was a `single-context` pre-check.

## Token Efficiency Rules
- Call `get_minimal_context_tool(task="<your task>")` before any other graph tool.
- Pass `detail_level="minimal"` wherever a tool accepts it. Use "standard" only when minimal is not enough.
- Prefer a targeted `query_graph_tool` call over a broad listing call.
- Budget: about five tool calls and 800 tokens of graph output per task.
- Read the implementation and its tests before changing code. The graph narrows scope; it does not replace the source.
