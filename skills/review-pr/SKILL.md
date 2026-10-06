---
name: review-pr
description: Review a GitHub pull request or branch diff using the knowledge graph for structural context, verify the change against base and head, and publish the review with gh. Outputs a structured review with blast-radius analysis. Covers first-pass reviews and re-review rounds that verify prior findings against a new head, resumable in a fresh session with --resume via the review's state ledger.
argument-hint: "[PR URL, number, or branch name] [--resume]"
hooks:
  Stop:
    - hooks:
        - type: command
          command: python
          args: ["${CLAUDE_PROJECT_DIR}/scripts/pr_watch_hook.py"]
          timeout: 20
  SessionStart:
    - hooks:
        - type: command
          command: python
          args: ["${CLAUDE_PROJECT_DIR}/scripts/pr_watch_hook.py"]
          timeout: 20
  UserPromptSubmit:
    - hooks:
        - type: command
          command: python
          args: ["${CLAUDE_PROJECT_DIR}/scripts/pr_watch_hook.py"]
          timeout: 20
---

# Review PR

Review a pull request or branch against its base: the graph picks what to
read, the base ref proves what the diff actually changed.

## State ledger

Every published review carries a machine-readable footer. It is the reason
re-review can be resumed: the thread is the state, so a fresh agent session
continues a round with no local file and no memory of the last one.

```html
<!-- gryphon-review-state {"v":1,"head":"964e6756f0b1c2d3e4f5a6b7c8d9e0f1a2b3c4d5","base":"staging","round":3,"flag":"request-changes","findings":[{"id":3,"status":"open","sev":"high","title":"Retry path skips the backoff","files":["src/net/retry.ts"]}]} -->
```

Build it with `scripts/pr_ledger.py` — never hand-write the JSON. That module
owns the escaping, the schema check, the resume decision and the delta
filter, all covered by `tests/test_pr_ledger.py`. Re-deriving them in prose
is how a `-->` in a finding title silently truncates the ledger.

```python
from pr_ledger import Finding, Ledger, decide, render, touches_open_findings

ledger = Ledger(
    head="<full 40-char sha>", base="staging", round=1, flag="comment",
    findings=(
        Finding(id=1, status="open", sev="high",
                title="Retry path skips the backoff",
                files=("src/net/retry.ts",)),
    ),
)
body = prose + "\n" + render(ledger) + "\n"
decision, reason = decide(ledger, current_head=sha, current_base="staging")
```

- One line, last thing in the review body. `head` is the full 40-char SHA the
  pass verified, `base` the base ref at that time, `round` 1-based, `flag`
  what was published (`comment`, `request-changes` or `approve`).
- `findings[].id` is stable for the whole PR. A finding opened as `#3` is
  still `#3` in round 9 after being resolved. New findings continue at
  `max(id) + 1` (`ledger.next_id()`); never renumber, never reuse an id.
- `status` is `open`, `resolved` or `obsolete`. Terminal states stay in the
  array — that is what stops a later round from re-reporting settled work.
  See *Ledger growth* for why compaction may not drop ids.
- `sev` is `high`, `medium` or `low`. `title` is one line. `files` lists what
  the finding touches, which is what makes the delta filter cheap.
- `render()` escapes `<`, `>` and `&` inside the JSON strings as
  backslash-u plus the 4-hex codepoint (U+003C, U+003E, U+0026). The block
  lives in an HTML comment, so a literal `-->` in a finding title would
  close it early. `parse_block` raises `LedgerError` on a block it cannot
  read; never degrade that to "no ledger", because that means round 1 and
  re-reports everything a previous round settled.
- `extract()` reads the **last** block in a body. The ledger is the last
  thing in a review, but a resuming agent can quote the previous body in
  its prose, so a stale copy may sit ahead of the current one. Never write
  a ledger into the prose yourself; `round_artifact.py` rejects it.
- Inspect an existing body without writing a round:
  `python scripts/pr_ledger.py <body-file> --current-head <sha> --current-base <ref>`.

## Ledger growth

The array grows for the life of the PR and is never pruned. Real PRs land
around 10ÔÇô15 findings, so this is fine until it is not, and the failure
mode should be fixed deliberately rather than mid-round.

Compaction must preserve **identity**, not just content: dropping a resolved
finding lets a later round re-raise it as new, and the author sees a fix they
already shipped rejected twice. Fold terminal runs into one anchor entry
that keeps the id reserved rather than dropping ids. `v` exists for this —
`decide` refuses an unknown version rather than guessing, so a format change
has somewhere safe to land.

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
  reviewed head, not the full file list. Read *State ledger* and
  *Re-review* before any graph call.

## Re-review: continuing a reviewed PR

When the PR already has review rounds (yours or others'), the unit of
work is the **open findings list**, not the full diff.

`review-pr <pr> --resume` runs this pass. It is stateless by design: the
ledger in the thread is the only thing carried between rounds, so the same
command works in a fresh session with no memory of the previous one.

1. **Pull the whole thread** — three different surfaces:
   - `gh api repos/<o>/<r>/pulls/<n>/reviews` ÔåÆ formal reviews (`state`,
     `body`, `submitted_at`)
   - `gh api repos/<o>/<r>/issues/<n>/comments` ÔåÆ conversation replies
     (the author's "fixed it" claims live here)
   - `gh api repos/<o>/<r>/pulls/<n>/comments` ÔåÆ inline review comments

   Walk the reviews newest-first. The ledger lands in either the first or
   the second surface — `gh pr review` submits a formal review, `gh pr
   comment` an issue comment — so merge both and take the newest block
   (`pr_ledger.latest`). Then `decide(...)` settles what this pass is;
   it returns the decision and a one-line reason, so report both:

   - **first-pass** ÔåÆ no ledger in the thread. Full pass, no resume
     shortcut, and say the ledger starts now. An edited-away block reads
     the same as a PR reviewed before ledgers existed.
   - **newer-schema** ÔåÆ an unknown `v`. Stop; the block is from a newer
     gryphon.
   - **retargeted** ÔåÆ `base` moved, which invalidates every finding.
     Full pass, new ledger, say why.
   - **already-reviewed** ÔåÆ `head` equals the ledger's. Report it and
     stop; never post a second review for a head already covered.
   - **rounds-exhausted** ÔåÆ the caller's `max_rounds` cap. Stop; the
     round summary is already in the thread.
   - **resume** ÔåÆ round `round + 1`. The open list is the array, the
     delta is `ledger.head..<current head>`.

   Author replies claiming a fix are claims, not evidence.
2. **Isolate the delta.** `git fetch origin <head>` + `git diff
   <ledger.head>..origin/<head>`. Without a block, fall back to the head
   SHA cited in the body, then to bounding by `submitted_at` against the
   commit list. Review that delta plus only the files in
   `ledger.open_files`. Merge commits inside the delta carry base-branch
   commits — they belong to the base, not to this review. Before any
   graph call, run `touches_open_findings(ledger, <delta files>)`: if it
   is False, no open finding can be resolved or regressed, so say the
   delta is irrelevant and stop before spending a pass on it.
3. **Verify each open finding at head.** Read the cited symbol at head.
   Resolved means: the fix matches one of the suggested resolutions
   *and* a regression test covers the behavior. A test that mocks the
   function under test proves nothing — check what it asserts. Also
   re-check "dead code" and "nobody calls this" claims from previous
   rounds: the fix may have revived the call path. Write the verdict
   against the carried id — `#3` stays `#3` — and move it to
   `resolved`, `obsolete` or leave it `open`.
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
   new; never re-list nits already raised unless they regressed. Carry
   every prior finding forward with its verdict and append the new ones
   to the same array — the ledger is the record, so a dropped id is a
   finding the next round will raise again.

## Unattended rounds

When no human is in the loop (the watcher, or the CI re-review job), the
agent proposes the flag and this policy decides what it may publish:

| Situation | Flag |
|---|---|
| A `high` finding an earlier round raised is **still open** at the new head | `request-changes` |
| A `high` finding this round raised for the first time | `comment` |
| A `low` or `medium` finding is open | `comment` |
| Nothing open, no new findings | publish nothing — no-op round |
| Merge recommendation positive | **never** `approve` |

The first row is a disconfirmation, not a disagreement: a blocker was
already raised, the author pushed, and it is verifiably still there. A
human reviewer blocks on that without re-deriving the analysis. The
second row is the bot's first disagreement with new code — an opinion, and
a merge blocked on an opinion costs more than one missed.

Deciding this requires the verdict be written against the **carried id**,
never re-derived from a fresh read of the diff.

The agent proposes a flag; the policy has the last word.
`pr_watch.escalate(previous_ledger, verdicts)` computes the allowed flag
and `round_artifact.resolve_flag(...)` is what the CI job calls. It derives
"was this finding already raised?" from the previous ledger's open ids
rather than from a field in the verdict — a field the model can forget to
set would downgrade every escalation to a comment, which is a failure that
looks like the system working. With no previous ledger the round can only
comment, and that is the safe direction.

Never escalate on `low`: nits and test gaps belong in follow-up issues,
not in a merge block. And never auto-approve — an unattended approve can
merge a PR nobody read. In interactive use nothing changes: the flag is
proposed in chat and published only after an explicit yes.

`pr_watch.render_round_body(ledger, verdicts, prose)` builds the body so a
round can never be published without the ledger the next round needs.

## Watching a PR

Publishing a review with `--request-changes` is the trigger. It means the
author now owes a correction, and that fact is already durable in two
places any later poll can read: the search index answers
`repo:<owner>/<name> reviewed-by:<you> is:pr is:open`, and the ledger in the
thread knows which findings are still open. So a watcher finds its own scope
by asking — **there is nothing to register and no label to apply**, which
means there is no step that can be forgotten.

A PR qualifies for a round when all three hold: you reviewed it, its ledger
still carries an open finding, and the head moved since that ledger was
written. When the round clears the last finding, the PR drops out on its
own.

Two ways to drive it, same scanner underneath.

**In the session you already have open** — the default, and what the
review-pr skill wires up for itself. `pr_watch_hook.py` runs at three
points and hands the pending round back to the agent:

| Event | Catches |
|---|---|
| `Stop` | the author pushed while you were reading; the round continues with no typing |
| `SessionStart` | you came back to the terminal and something landed while you were gone |
| `UserPromptSubmit` | you asked something and a push is waiting |

All three accept `additionalContext`, which is the only channel they share.
It is the non-error one: the transcript labels it "Stop hook feedback", not
a hook error. `UserPromptSubmit` is the delicate one — it blocks model
processing until the hook returns, and a hook that times out there has its
context *discarded*, so the poll is throttled and shared across all three
events. One poll per interval, whichever event arrived first.

Nothing runs while you are away and nothing posts under your name while you
sleep. The round happens where you can watch it, interrupt it, or redirect
it. What it removes is the remembering.

**Headless**, for when you want it to run without the session:
`review_watch.py` polls and invokes the agent itself. Same `survey()`, same
escalation check, plus a lock so two polls cannot post two reviews for one
head. Opt in deliberately:

```bash
python scripts/review_watch.py --repo <owner>/<name> --dry-run
python scripts/review_watch.py --register --repo <owner>/<name> --interval-minutes 15
python scripts/review_watch.py --unregister
```

**In CI**, for PRs where nobody has a session open: `pr_rereview.yml`
resumes on `synchronize` and `round_artifact.py` composes the body for the
privileged job to post.

`pr_watch.py` is the scanner both the hook and the runner call, so the
decision is free and identical wherever it is made:

```bash
python scripts/pr_watch.py --repo <owner>/<name> --json --max-rounds 6
python scripts/pr_watch.py --repo <owner>/<name> --check   # exit 1 when ready
```

Exit codes: `0` nothing to do, `1` at least one PR is ready, `2` the
repository could not be queried, `3` a thread carried an unreadable ledger.

Pass `--label <name>` to narrow the scan, which is how one PR gets paused
while the rest keep running. The default is every PR you reviewed.

The watcher follows you across repositories. `GRYPHON_PR_WATCH_REPO` takes
a comma-separated list, and each repo is polled in turn:

```
GRYPHON_PR_WATCH_REPO=Ativos-Tecnologia/cvld,jjgouveia/gryphon-standalone
```

With it unset, the hook uses the checkout's own `origin`, which is right
for a session opened inside the repository being reviewed. One repo
failing does not disable the others, but a poll where *every* repo fails
is reported rather than answered with "nothing to do" — a mistyped list
must not look like a quiet afternoon.

`GRYPHON_PR_WATCH_INTERVAL` (seconds, default 45) throttles the poll. It
is shared across the three events, so three turns inside a minute cost one
poll, not three.

Running locally is strictly better than in CI for the code itself: it has
the checkout, the virtualenv and the graph, so it can run the affected tests
from step 4, and its reviews are attributed to you rather than to
`github-actions[bot]`.

What the headless runner guards:

- **One round at a time.** Task Scheduler will start a second instance
  while the first runs, and two agents on one PR post two reviews for one
  head. A lock file keyed on the pid prevents it, and a lock left by a
  killed process is reclaimed — a lock that wedges looks exactly like
  "nothing to review" forever.
- **The published flag is checked afterwards.** The runner reads the thread
  back and compares against `pr_watch.escalate`. An agent that published
  `request-changes` without a carried blocker is reported as a policy
  violation rather than left blocking a merge quietly.
- **A head that moved mid-round** is noted, not treated as failure; the
  ledger records what was reviewed and the next poll takes the new head.

The CI round needs `pull-requests: read` on the analysis job: `contents:
read` grants no such scope, so the ledger lookup would come back empty and
every PR would look like round 1.

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
2. `detect_changes_tool(changed_files=<list>, detail_level="minimal")` ÔåÆ
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
- Ask whether to publish. Only after an explicit yes, publish through
  `scripts/publish_review.py`, which composes the ledger into the body and
  refuses a body it cannot read back:

  ```bash
  python scripts/publish_review.py \
    --repo <owner>/<repo> --pr <n> --flag <comment|request-changes|approve> \
    --prose <prose.md> --verdicts <verdicts.json> \
    --head <sha> --base <ref>
  ```

  `--verdicts` is `{"findings": [...]}` with the same shape the ledger
  uses; omit it for a review with no findings. The round number is derived
  from the thread, so a re-review continues the numbering. `--dry-run`
  composes and verifies without posting.

  Never publish with `gh pr review` directly. That was the documented way
  and it silently produced reviews with no ledger, which the watcher then
  could not resume — the loop died on exactly the reviews worth writing,
  and nothing complained at the time.
- The prose goes in `--prose`, not in the body file: if the prose contains
  a `<!-- gryphon-review-state` block the script refuses, because a second
  ledger would make the reader pick the wrong one.
- The script posts a comment instead of a review on a merged or closed PR.
- If the user edits the draft, apply the edit verbatim and re-show the
  body before publishing.
- Findings that would warrant a "before merging I'd change X" but are
  not blockers (test gaps, latent footguns, follow-up refactors) become a
  GitHub issue assigned to the PR author
  (`gh issue create --repo <owner>/<repo> --assignee <author-login>`) for
  a follow-up PR — the review does not hold the merge over them and only
  references the issue. Draft the issue in chat first and create it only
  after an explicit yes. Match the repo's issue conventions — title
  prefix, sections, language — by checking a recent issue from the same
  team first.
- **Never file a follow-up issue before checking what's already tracked.**
  Search open issues for the finding's theme:
  `gh issue list --repo <o>/<r> --state open --search "<keywords>"`.
  Read the issues the PR body references too (`Closes #n`, `Refs`,
  `Referencia`) — they delimit the PR's mandate, and debt from prior
  reviews often already lives there (e.g. a "review follow-up" issue
  covering the exact method your finding names). If an open issue covers
  the finding, comment the new deltas on it instead of filing a
  duplicate; create a new issue only for what nothing covers.

## Output

Group findings by risk (high, medium, low). Number every finding — that
number is its id for the rest of the PR, so it is assigned once and never
changes. For each finding give what changed and why it matters, its test
coverage, and the suggested fix. End with a merge recommendation. Keep the
structure light — a few strong paragraphs beat a long checklist.

## Graph metrics

After the review, report in chat (never in the PR body): which graph tools
ran, `context_savings.saved_tokens`/`saved_percent`, test gaps the graph
surfaced, and files the graph surfaced that a grep would have missed.

## Tips

- `semantic_search_nodes_tool` finds related code the PR may have missed.
- `gryphon measure --base <base> --head <head> --ref "PR #<n>"` logs a
  counterfactual savings entry for the `gryphon savings` dashboard.
