---
name: review-multi
description: Review a pull request or branch diff with two independent reviewers run in parallel, merge their findings, check each one against the knowledge graph and rank them. Use when a change is large or risky enough that one pass will miss things; it costs about twice a single review.
argument-hint: "[PR URL, number, or branch name]"
---

# Review Multi

One review misses real defects that a second, independent one finds. In a
benchmark of 19 closed pull requests, two baseline reviews merged found
3.4 more real defects than one and raised pooled recall by 16 points, at
twice the cost. This workflow does that merge on purpose, then uses the
graph to check and order the result.

The graph does not find defects here. The two reviewers do. The graph
decides which findings to trust first.

## 1. Resolve the change set

Same as `review-pr`: for a GitHub PR take `gh pr view <n> --json
title,body,baseRefName,headRefName,files`, and use `files[].path` as the
change set; for a local branch use `git diff --name-only <base>...<branch>`.
Never rely on graph auto-detect for a remote PR.

Call `get_minimal_context_tool(task="review PR #<n>", changed_files=<list>)`
once. If it reports a stale graph built on another branch, rebuild as
`review-pr` section 2 says before going on.

## 2. Two independent reviewers, in parallel

Start two subagents in the same message, each with a fresh context. Give
both the same brief:

- the repository path, base and head refs, the change set, and the PR
  title and description;
- the task: find correctness bugs, regressions and missing or wrong tests
  in the change; verify each in the source (`git show <base>:<path>` for
  the old state) before reporting it;
- the answer format, one finding per item: file, line, severity
  (`blocker`, `major`, `minor`), category, the claim, and the evidence
  (the line or the call path that shows it).

Neither reviewer sees the other's work, and neither is told about the
graph.

Reviewer B gets one more instruction: before concluding, read the tests
that exercise the changed symbols. Reviews that had graph context
explored tests less and reported fewer test gaps.

If the platform cannot run two subagents, do the review twice in separate
sessions and bring both lists back here. A second pass that has read the
first one is not independent and adds little.

## 3. Merge

Combine the two lists. Two findings are the same when they describe the
same defect in the same code, not merely the same line. For each merged
finding keep who reported it: A, B or both.

Do not drop a finding because only one reviewer reported it. Half of the real
defects the baseline reviews found (43 of 86) came from one review only.

## 4. Check each finding with the graph

Check by what the finding claims. About 2 graph calls per finding, 10 in
total; skip the check for a finding the source already settles. Pass
`detail_level="minimal"` to every graph call that takes it.

| The finding says | Check | Result |
| --- | --- | --- |
| nothing calls X, X is dead | `query_graph_tool(pattern="callers_of", target="X")` | callers found: the claim is contradicted |
| a caller Y breaks | `callers_of` the changed symbol | Y is not a caller: ask the reviewer to re-check |
| X has no test | `query_graph_tool(pattern="tests_for", target="X")`, then grep the tests | a test covers it: contradicted |
| the change affects other code | `review_diff_tool(base=<base>)` or `get_impact_radius_tool` | list the callers outside the diff |

The graph is static. Callers reached through signals, decorators and
dynamic dispatch are missing from it, so an empty `callers_of` does not
prove a function is unused: grep the name before accepting it.

**Never delete a finding.** Mark one `contradicted` only when the graph or
the source shows the claim is wrong, and say what showed it. A finding
the graph cannot check goes under *not verified*, with its claim intact.

## 5. Rank and write

Order by severity, then by reported by both, then by the number of
callers outside the diff (more callers first). Open with the three
highest, then the rest, then *not verified*, then *contradicted* with
the reason for each.

For each finding give where it is, what is wrong, the evidence, who
reported it, and what the graph showed. End with a merge recommendation.

Follow `review-pr` section 5 to publish: show the draft in chat first and
publish only after an explicit yes.

## Graph metrics

After the review, report in chat (never in the PR body): how many findings
each reviewer reported alone and together, how many the graph contradicted,
and which graph calls ran.
