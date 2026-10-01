"""The review prompt and the findings schema both arms answer with.

The two arms get the same task, rules and output contract. Only the
``tools`` section differs: the graph arm is told how to use the gryphon MCP
tools, the baseline arm gets the equivalent git/grep workflow. Bump
``PROMPT_VERSION`` on any change to what either arm is told (this prompt, or
the instructions and hooks ``runner.build_command`` adds to the graph arm) so
runs stay comparable.
"""

from __future__ import annotations

from .sandbox import BASE_BRANCH, REVIEW_BRANCH

PROMPT_VERSION = "3"

SEVERITIES = ("blocker", "major", "minor")
CATEGORIES = (
    "bug", "regression", "broken-caller", "test-gap", "security",
    "performance", "data", "other",
)

FINDINGS_SCHEMA: dict = {
    "type": "object",
    "additionalProperties": False,
    "required": ["summary", "findings"],
    "properties": {
        "summary": {"type": "string"},
        "findings": {
            "type": "array",
            "items": {
                "type": "object",
                "additionalProperties": False,
                "required": ["file", "line", "severity", "category", "claim", "evidence"],
                "properties": {
                    "file": {"type": "string"},
                    "line": {"type": ["integer", "null"]},
                    "severity": {"enum": list(SEVERITIES)},
                    "category": {"enum": list(CATEGORIES)},
                    "claim": {"type": "string"},
                    "evidence": {"type": "string"},
                },
            },
        },
    },
}

_TASK = """\
You are reviewing a pull request in the Git repository in the current directory.

The PR branch `{review}` is checked out. It merges into `{base}`; the change
under review is `git diff {base}...{review}`.

Title: {title}

Description:
{body}

Changed files (added/deleted lines):
{files}

Find the defects this change introduces: bugs, regressions, callers outside
the diff that the change breaks, missing or wrong tests for changed behavior,
security and data-integrity problems. Read the pre-change code on `{base}`
(`git show {base}:<path>`) before claiming something changed or broke.

Rules:
- Report only what you verified in the code. No style nits, no speculative
  refactors, no praise.
- Do not modify, create or delete files, and do not commit.
- There is no network access. Do not try to reach GitHub or any remote.
- An empty findings list is a valid answer when the change is sound.

{tools}

Return the review through the structured output: a short `summary` and one
entry per finding with `file` (repo-relative path), `line` (at `{review}`, or
null), `severity` (blocker, major or minor), `category`, `claim` (what is
wrong, one or two sentences) and `evidence` (the code that proves it). Write
`summary`, `claim` and `evidence` in the language of the PR title.
"""

_TOOLS_BASELINE = """\
How to explore: use git, grep and file reads.
1. `git diff --stat {base}...{review}`, then read the diff file by file.
2. For each changed function, class or public contract, grep for its call
   sites and subclasses outside the diff and check they still hold.
3. Locate the tests that cover the changed behavior and check what they
   assert."""

_TOOLS_GRAPH = """\
How to explore: a code knowledge graph of this repository, built at
`{review}`, is available through the gryphon MCP tools. Use it to decide what
to read, then verify in the source.
1. `get_minimal_context_tool(task="review PR", changed_files=<changed files>,
   base="{base}")` for the overview.
2. `detect_changes_tool(base="{base}", changed_files=<changed files>,
   detail_level="minimal")` for risk, test gaps and affected flows.
3. For each high-risk function: `query_graph_tool(pattern="callers_of",
   target=<fn>)` finds call sites the diff does not show, and
   `pattern="tests_for"` checks whether a reported test gap is real.
4. Escalate to `get_review_context_tool` or `get_impact_radius_tool` only
   when a high-risk item stays unclear. Budget: about 5 graph calls.
The graph can be incomplete: an empty result means "not statically
visible", not "does not exist". The source wins when they disagree."""


_TOOLS_GRAPH_REQUIRED = _TOOLS_GRAPH + """

Required protocol for this review — follow it even if the diff looks small:
- Your first two tool calls must be `get_minimal_context_tool` and
  `detect_changes_tool`, in that order, before any git, grep or file read.
- For every changed function or class you report on, and for every
  high-risk function `detect_changes_tool` lists, call `query_graph_tool`
  with `callers_of` (and `tests_for` when test coverage is in question)
  before concluding.
- Then verify each finding in the source as usual."""

TOOLS_BY_ARM = {
    "baseline": _TOOLS_BASELINE,
    "graph": _TOOLS_GRAPH,
    "graph_required": _TOOLS_GRAPH_REQUIRED,
    "graph_md": _TOOLS_GRAPH,
    "graph_md_enrich": _TOOLS_GRAPH,
    "graph_install": _TOOLS_GRAPH,
    "graph_install_ref": _TOOLS_GRAPH,
}


def _format_files(files: list[dict]) -> str:
    if not files:
        return "(none)"
    lines = []
    for f in files:
        added = "-" if f.get("added") is None else f"+{f['added']}"
        deleted = "-" if f.get("deleted") is None else f"-{f['deleted']}"
        lines.append(f"- {f['path']} ({added}/{deleted})")
    return "\n".join(lines)


def build_prompt(arm: str, *, title: str, body: str, files: list[dict]) -> str:
    """The full review prompt for *arm*."""
    tools = TOOLS_BY_ARM[arm]
    fmt = {"base": BASE_BRANCH, "review": REVIEW_BRANCH}
    return _TASK.format(
        title=title.strip() or "(no title)",
        body=body.strip() or "(no description)",
        files=_format_files(files),
        tools=tools.format(**fmt),
        **fmt,
    )
