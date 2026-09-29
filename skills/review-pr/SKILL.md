---
name: review-pr
description: Review a GitHub pull request or branch diff using the knowledge graph for structural context, verify the change against base and head, and publish the review with gh. Outputs a structured review with blast-radius analysis. Covers first-pass reviews and re-review rounds that verify prior findings against a new head.
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
- **PR already reviewed?** The change set is the delta since the last
  reviewed head, not the full file list. See *Re-review* below before
  any graph call.

## Re-review: continuing a reviewed PR

When the PR already has review rounds (yours or others'), the unit of
work is the **open findings list**, not the full diff.

1. **Pull the whole thread** — three different surfaces:
   - `gh api repos/<o>/<r>/pulls/<n>/reviews` → formal reviews (`state`,
     `body`, `submitted_at`)
   - `gh api repos/<o>/<r>/issues/<n>/comments` → conversation replies
     (the author's "fixed it" claims live here)
   - `gh api repos/<o>/<r>/pulls/<n>/comments` → inline review comments

   Walk the reviews newest-first and extract each numbered finding as
   open/resolved/obsolete. Author replies claiming a fix are claims,
   not evidence.
2. **Isolate the delta.** Prior review bodies usually cite the head
   SHA they verified (e.g. `head 964e6756`); otherwise bound it by
   `submitted_at` against the commit list. Then `git fetch origin
   <head>` + `git diff <old-head>..origin/<head>`. Review that delta
   plus only the files the open findings touch. Merge commits inside
   the delta carry base-branch commits — they belong to the base, not
   to this review.
3. **Verify each open finding at head.** Read the cited symbol at head.
   Resolved means: the fix matches one of the suggested resolutions
   *and* a regression test covers the behavior. A test that mocks the
   function under test proves nothing — check what it asserts. Also
   re-check "dead code" and "nobody calls this" claims from previous
   rounds: the fix may have revived the call path.
4. **Re-run what was red first.** Tests that failed in previous rounds
   are the cheapest oracle — run them before the broader suite. Lint
   the touched files too (`ruff check --select F821` catches
   missing-import breakage without booting the app).
5. **Expect breakage inside the fix.** Fix commits often introduce new
   issues (a re-added call path drops an import, a restored fan-out
   drops the debounce). Diff the delta fresh; don't assume a fix
   commit is only a fix.
6. **Body continuity.** Title the pass ("segunda passada", "quarta
   passada"), open with per-item resolution status keyed to the prior
   numbering, then new findings. Never re-report a resolved item as
   new; never re-list nits already raised unless they regressed.

## 2. Graph pass

1. `get_minimal_context_tool(task="review PR #<n>", changed_files=<list>)`.
   - On `status: not_ready` with `reason: stale_graph`:
     - Check `built_on_branch`/`built_at_sha`.
     - If the graph was built on the **PR base branch** or an ancestor of
       it, it is usable — continue with explicit `changed_files`.
     - If the graph was built on a **different branch** (e.g. `new-main`
       while reviewing a PR against `staging`), the graph nodes belong to
       a different domain. **Rebuild in the worktree before continuing:**
       ```bash
       git checkout <PR_base_branch>
       gryphon build
       git checkout -   # return to original branch
       ```
       Or, for large repos where checkout is expensive, create a worktree:
       ```bash
       git worktree add .wt-pr<n> origin/<PR_base_branch>
       gryphon build --repo .wt-pr<n>
       ```
       Then pass `repo_root` explicitly to all graph calls.
     - Only skip the rebuild when the build commit is gone from the clone
       (history rewrite / shallow fetch) — in that case, run
       `build_or_update_graph_tool(full_rebuild=true, postprocess="minimal")`.
   - On `status: ok` but `_graph.built_on_branch` differs from the PR base,
     warn in chat but continue: the graph is usable for structural queries
     but `detect_changes` risk scores may be off.
2. `detect_changes_tool(changed_files=<list>, detail_level="minimal")` →
   risk score, test gaps, affected flows. If the graph was just rebuilt,
   the risk scores now reflect the correct domain.
3. For each high-risk function:
   `query_graph_tool(pattern="callers_of", target="<fn>")` finds call sites
   the diff does not show (inheritance, dynamic calls, signals), and
   `pattern="tests_for"` confirms whether a reported test gap is real.
   Flag public-API changes whose callers were not updated.
4. Escalate to `get_review_context_tool(detail_level="minimal")` or
   `get_impact_radius_tool` only when a high-risk item stays unclear.
   Budget: ~5 graph calls per review (excluding the rebuild).

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
- Pertinent findings that do not block the merge (test gaps, latent
  footguns, follow-up refactors) deserve tracking, not just a paragraph in
  the review. Offer to open a GitHub issue assigned to the PR author
  (`gh issue create --repo <owner>/<repo> --assignee <author-login>`).
  Draft the issue in chat first and create it only after an explicit yes.
  Match the repo's issue conventions — title prefix, sections, language —
  by checking a recent issue from the same team first.

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
