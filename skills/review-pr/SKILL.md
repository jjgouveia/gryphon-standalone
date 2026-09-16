---
name: review-pr
description: Review a GitHub pull request or branch diff using the knowledge graph for structural context, verify the change against base and head, and publish the review with gh. Outputs a structured review with blast-radius analysis.
argument-hint: "[PR URL, number, or branch name]"
---

# Review PR

Review a pull request or branch against its base: the graph picks what to
read, the base ref proves what the diff actually changed.

## 1. Resolve the change set

- **GitHub PR** (URL or number):
  `gh pr view <n> --repo <owner>/<repo> --json title,body,author,baseRefName,headRefName,files,additions,deletions,state,commits`.
  The `files[].path` list is the authoritative change set — pass it to every
  graph call below as `changed_files`. Note `baseRefName` (the base) and
  `headRefName`.
- **Local branch**: `git diff --name-only <base>...<branch>` and use `<base>`
  as `base` in graph calls.
- Never rely on graph auto-detect for a remote PR: it diffs the working
  tree, which may sit on an unrelated commit.

## 2. Graph pass

1. `get_minimal_context_tool(task="review PR #<n>", changed_files=<list>)`.
   On `status: not_ready` with `reason: stale_graph`, check
   `built_on_branch`/`built_at_sha` first: a graph built on the PR base is
   usable — continue with explicit `changed_files`. Only call
   `build_or_update_graph_tool` (incremental, `postprocess="minimal"` if
   slow) when the build is missing or its commit is gone from the clone.
2. `detect_changes_tool(changed_files=<list>, detail_level="minimal")` →
   risk score, test gaps, affected flows.
3. For each high-risk function:
   `query_graph_tool(pattern="callers_of", target="<fn>")` finds call sites
   the diff does not show (inheritance, dynamic calls, signals), and
   `pattern="tests_for"` confirms whether a reported test gap is real.
   Flag public-API changes whose callers were not updated.
4. Escalate to `get_review_context_tool(detail_level="minimal")` or
   `get_impact_radius_tool` only when a high-risk item stays unclear.
   Budget: ~5 graph calls per review.

## 3. Verify against base and head

- `gh pr diff <n>` is the real diff; cross-check the file count against
  step 1.
- Read the pre-change state on the base ref:
  `git show origin/<base>:<path>` or
  `gh api repos/<owner>/<repo>/contents/<path>?ref=<base>`. Never claim
  "already did X" or "nobody called Y" without base evidence.
- Judge the diff against the PR description and code, not against the
  author's narrative alone.
- `gh pr checks <n>` informs you; only mention CI in the review when a
  failure is clearly caused by this diff.
- Large remote PRs: review in a worktree (`git fetch origin <head>` +
  `git worktree add ../.wt-pr<n> origin/<head>`), not in a dirty workspace.
  Reuse the parent checkout's virtualenv and env files instead of
  reinstalling.

## 4. Tests (when the environment allows)

- If the PR touches testable behavior, run the affected domain tests before
  requesting new tests or approving.
- To run tests that only exist in the head, apply
  `git diff origin/<base>...origin/<head> | git apply --whitespace=nowarn -`
  onto a base worktree first.
- If you could not run tests, say so in chat; never imply they passed.
- Do not request tests, docs or refactors the diff already includes —
  check the file list first. Out-of-scope suggestions belong to an
  optional follow-up note.

## 5. Draft, then ask, then publish

- Show the review body in chat first, with the suggested flag
  (`--comment` / `--request-changes` / `--approve`) stated next to the
  draft — never inside the body.
- Ask whether to publish. Only after an explicit yes:
  `gh pr review <n> --repo <owner>/<repo> --<flag> --body-file <file>`.
  Write the body file UTF-8 without BOM so accents survive.
- On merged or closed PRs `gh pr review` is rejected — use
  `gh pr comment <n> --body-file <file>` instead.
- If the user edits the draft, apply the edit verbatim and re-show the
  body before publishing.

## Output

Group findings by risk (high, medium, low). For each finding give what
changed and why it matters, its test coverage, and the suggested fix.
End with a merge recommendation. Keep the structure light — a few strong
paragraphs beat a long checklist.

## Graph metrics

After the review, report in chat (never in the PR body): which graph tools
ran, `context_savings.saved_tokens`/`saved_percent`, test gaps the graph
surfaced, and files the graph surfaced that a grep would have missed.

## Tips

- `semantic_search_nodes_tool` finds related code the PR may have missed.
- `gryphon measure --base <base> --head <head> --ref "PR #<n>"` logs a
  counterfactual savings entry for the `gryphon savings` dashboard.
