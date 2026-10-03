---
name: review-multi
description: Review a pull request or branch diff with two independent reviewers run in parallel, merge their findings into one ranked list and check each finding against the code. Use when a change is large or risky enough that one pass will miss things; it costs about two and a half single reviews.
argument-hint: "[PR URL, number, or branch name]"
---

# Review Multi

One review misses real defects that a second, independent one finds. In a
benchmark of 19 closed pull requests, two reviews together covered 22
points more of the real defects than one review alone, and a merge step
turned their 7.6 findings per case into 5.1 with the same coverage and
fewer false ones. This workflow does that on purpose.

Checking findings with the knowledge graph gave the same result as checking
them in the source (no change in recall, false findings or precision), so
the graph is an option for the check, not a requirement.

## 1. Resolve the change set

Same as `review-pr`: for a GitHub PR take `gh pr view <n> --json
title,body,baseRefName,headRefName,files`, and use `files[].path` as the
change set; for a local branch use `git diff --name-only <base>...<branch>`.
Never rely on graph auto-detect for a remote PR.

If the gryphon tools are available, `get_minimal_context_tool(task="review
PR #<n>", changed_files=<list>, detail_level="minimal")` once tells you
whether the graph is current; rebuild as `review-pr` section 2 says if it
was built on another branch.

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

Neither reviewer sees the other's work.

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

Do not drop a finding because only one reviewer reported it. Half of the
real defects the benchmark's reviews found (43 of 86) came from one review
only.

## 4. Check each finding

Check by what the finding claims, in the source: about two lookups per
finding, ten in total, and none for a finding the source already settles.

| The finding says | Check |
| --- | --- |
| nothing calls X, X is dead | grep the repository for X, including tests, registrations and string references; callers found contradict the claim |
| a caller Y breaks | read Y and the changed code; confirm the call is real |
| X has no test | grep the tests for X and read what they assert; a test that covers X contradicts the claim |
| the change affects other code | list the callers outside the diff |

The gryphon tools do the same lookups faster when they are available:
`query_graph_tool(pattern="callers_of", target=X, detail_level="minimal")`,
`pattern="tests_for"`, and `review_diff_tool(base=<base>)` for the changed
symbols called from outside the diff. The graph is static: callers reached
through signals, decorators and dynamic dispatch are missing, so an empty
`callers_of` does not prove X is unused. Grep the name before you accept it.

**Never delete a finding.** Mark one `contradicted` only when the source or
the graph shows the claim is wrong, and say what showed it. A finding you
could not check goes under *not verified*, with its claim intact. In the
benchmark this step set aside 2 findings in 19 cases, and both were false.

## 5. Rank and write

Order by severity, then by reported by both, then by the number of
callers outside the diff (more first). Open with the three highest, then
the rest, then *not verified*, then *contradicted* with the reason for
each. Ranked this way, the first three held 0.34 more serious defects
than a single review's own order.

For each finding give where it is, what is wrong, the evidence, and who
reported it. End with a merge recommendation.

Follow `review-pr` section 5 to publish: show the draft in chat first and
publish only after an explicit yes.

## Report in chat

After the review, say in chat (never in the PR body): how many findings
each reviewer reported alone and together, how many were set aside as
contradicted, and, if the graph tools ran, which calls.
