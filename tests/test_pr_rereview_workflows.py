"""Static security regressions for the unattended re-review workflows."""

from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).parents[1]
ANALYSIS_WORKFLOW = ROOT / ".github" / "workflows" / "pr-rereview.yml"
PUBLISH_WORKFLOW = ROOT / ".github" / "workflows" / "pr-rereview-comment.yml"
REFERENCE = ROOT / ".github" / "workflows" / "pr-review.yml"
SKILL = ROOT / "skills" / "review-pr" / "SKILL.md"


def unwrap(text: str) -> str:
    """Collapse YAML line wrapping so assertions can span lines.

    A `run:` block's shell and prompt text is hard-wrapped for the file;
    asserting on the folded source would couple the test to the wrapping.
    """
    return " ".join(text.split())


@pytest.fixture(scope="module")
def analysis() -> dict:
    return yaml.safe_load(ANALYSIS_WORKFLOW.read_text(encoding="utf-8"))


@pytest.fixture(scope="module")
def publish() -> dict:
    return yaml.safe_load(PUBLISH_WORKFLOW.read_text(encoding="utf-8"))


def jobs(workflow: dict) -> dict:
    return workflow["jobs"]


def test_no_workflow_level_env_uses_the_runner_context(analysis, publish):
    # `runner` is not an available context at the workflow level. Referencing
    # it there makes GitHub reject the whole file at validation time, which
    # surfaces as a run that fails in 0s with no jobs and no log — the
    # failure mode that looks like nothing happened at all.
    for workflow in (analysis, publish):
        for key, value in (workflow.get("env") or {}).items():
            assert "runner." not in str(value), (
                f"workflow-level env {key!r} uses the runner context"
            )


def test_every_expression_uses_a_context_available_where_it_sits(analysis, publish):
    # The workflow-level `on`/`env`/`concurrency` may use `github` and `vars`;
    # `runner` and `steps` belong to jobs. This is a coarse guard: it catches
    # the context being hoisted too high, which is the mistake that is
    # invisible locally and fatal in CI.
    for workflow in (analysis, publish):
        for value in (workflow.get("env") or {}).values():
            assert "steps." not in str(value)
        group = (workflow.get("concurrency") or {}).get("group")
        if group:
            assert "runner." not in str(group)
            assert "steps." not in str(group)


# --- both workflows are valid YAML with the expected shape --------------


def test_both_workflows_parse_and_name_their_jobs(analysis, publish):
    assert set(jobs(analysis)) == {"round"}
    assert set(jobs(publish)) == {"publish"}
    assert analysis["name"] == "PR Re-review"
    assert publish["name"] == "PR Re-review Comment"


def test_the_analysis_job_is_paused(analysis):
    # The CI round is off: it needs an agent key this repo does not carry,
    # and the round runs in the reviewer's own session instead. Nothing may
    # start it automatically, and a manual dispatch is the deliberate way
    # back in.
    triggers = analysis[True] if True in analysis else analysis["on"]

    assert "pull_request" not in triggers
    assert list(triggers) == ["workflow_dispatch"]


def test_the_paused_workflow_still_documents_how_to_resume_it(analysis):
    text = ANALYSIS_WORKFLOW.read_text(encoding="utf-8")

    assert "Paused." in text
    assert "types: [synchronize]" in text, (
        "the trigger to restore must stay visible in the comment"
    )


def test_the_publish_job_is_driven_by_workflow_run(publish):
    triggers = publish[True] if True in publish else publish["on"]

    assert triggers["workflow_run"]["workflows"] == ["PR Re-review"]
    assert triggers["workflow_run"]["types"] == ["completed"]


# --- the privileged boundary --------------------------------------------


def test_the_agent_job_is_unprivileged(analysis):
    text = ANALYSIS_WORKFLOW.read_text(encoding="utf-8")

    assert "pull-requests: write" not in text
    assert "issues: write" not in text
    # It must not be able to publish, however indirectly.
    assert "gh pr review" not in text
    assert "gh api --method POST" not in text
    assert "pulls/${{ github.event.pull_request.number }}/reviews" not in text


def test_the_agent_job_can_read_the_review_thread(analysis):
    text = ANALYSIS_WORKFLOW.read_text(encoding="utf-8")

    # pr_watch.py and round_artifact.py both read the thread to find the
    # previous ledger. `contents: read` grants no pull-requests scope, so
    # without this every lookup is empty, every PR looks like round 1, and
    # the loop reports itself healthy while reviewing nothing.
    assert "contents: read" in text
    assert "pull-requests: read" in text
    assert "pull-requests: write" not in text
    # The escalation rule needs it too, so the repo is passed through.
    assert '--repo "${GITHUB_REPOSITORY}"' in text


def test_the_agent_finds_the_skill_it_is_told_to_read(analysis):
    text = ANALYSIS_WORKFLOW.read_text(encoding="utf-8")

    # `.claude/` is gitignored, so a fresh checkout has no skills there. The
    # tracked copy lives at `skills/`; the workflow has to stage it before
    # the prompt points the agent at `.claude/`.
    stage = text.index("cp -R skills/review-pr .claude/skills/review-pr")
    prompt = text.index("Invoke the review-pr skill from .claude/skills/review-pr/SKILL.md")

    assert stage < prompt
    assert "mkdir -p .claude/skills" in text


def test_the_publish_job_never_checks_out_pr_code(publish):
    text = PUBLISH_WORKFLOW.read_text(encoding="utf-8")

    assert "actions/checkout" not in text
    assert "uses: ./" not in text
    assert "actions: read" in text
    assert "pull-requests: write" in text
    assert "default branch" in text


def test_the_publish_job_refuses_a_run_whose_head_moved(publish):
    text = PUBLISH_WORKFLOW.read_text(encoding="utf-8")

    assert "workflow_run.head_sha" in text
    assert "no longer the head" in text


def test_the_publish_job_refuses_a_mismatched_ledger(publish):
    text = PUBLISH_WORKFLOW.read_text(encoding="utf-8")

    assert "gryphon-review-state" in text
    assert "does not match the analysed commit" in text
    assert "does not match the metadata" in text


def test_the_publish_job_confines_the_untrusted_body(publish):
    text = PUBLISH_WORKFLOW.read_text(encoding="utf-8")

    assert "MAX_ARCHIVE_BYTES" in text
    assert "MAX_REPORT_BYTES" in text
    assert "MAX_BODY_BYTES" in text
    assert "is_symlink" in text
    assert 'decode("utf-8")' in text
    assert 'text.replace("@", "&#64;")' in text


def test_an_unattended_round_may_not_approve(publish):
    text = PUBLISH_WORKFLOW.read_text(encoding="utf-8")

    assert 'flag not in ("comment", "request-changes")' in text
    assert "not permitted from an unattended round" in text


def test_a_missing_artifact_is_the_normal_case_not_a_failure(publish):
    text = PUBLISH_WORKFLOW.read_text(encoding="utf-8")

    # A PR with nothing to re-review produces no artifact, and that must
    # not fail the workflow or leave it stuck waiting.
    assert "publish=false" in text
    assert "needed no re-review round" in text


# --- the pre-filter runs before the agent, and caps rounds --------------


def test_the_pre_filter_gates_the_agent(analysis):
    text = ANALYSIS_WORKFLOW.read_text(encoding="utf-8")

    decide_at = text.index("pr_watch.py")
    agent_at = text.index("@anthropic-ai/claude-code")

    assert decide_at < agent_at
    assert "--max-rounds 6" in text
    assert "needs-round" in text


def test_the_pre_filter_checks_the_event_pr_not_a_reviewer_search(analysis):
    text = ANALYSIS_WORKFLOW.read_text(encoding="utf-8")

    # The workflow token is an integration token: `gh api user` answers it
    # with 403, so a reviewer-scoped scan fails on its first call. The
    # event already names the PR, and the ledger proves it was reviewed.
    assert '--pr "${PR_NUMBER}"' in text
    assert "--reviewer" not in text
    assert "github.event.pull_request.number" in text


def test_the_body_is_composed_by_the_script_not_the_model(analysis):
    text = ANALYSIS_WORKFLOW.read_text(encoding="utf-8")

    # The ledger is rebuilt from verdicts.json, so a model that forgot the
    # escaping rule cannot publish a corrupt ledger.
    assert "round_artifact.py" in text
    assert "verdicts.json" in text
    assert "Do not edit the ledger itself" in unwrap(text)


def test_the_prompt_forbids_the_agent_from_publishing(analysis):
    text = unwrap(ANALYSIS_WORKFLOW.read_text(encoding="utf-8"))

    assert "you may never publish anything yourself" in text
    assert "You may never approve" in text


def test_the_prompt_keeps_every_carried_id(analysis):
    text = unwrap(ANALYSIS_WORKFLOW.read_text(encoding="utf-8"))

    assert "Every id from the previous ledger must appear exactly once" in text
    assert "max(id) + 1" in text


# --- round discipline ---------------------------------------------------


def test_concurrent_rounds_on_one_pr_are_cancelled(analysis):
    concurrency = analysis["concurrency"]

    assert "cancel-in-progress" in concurrency
    assert "github.event.pull_request.number" in concurrency["group"]


def test_fork_prs_are_skipped_rather_than_computed_and_dropped(analysis):
    condition = jobs(analysis)["round"]["if"]

    assert "head.repo.full_name == github.repository" in condition


def test_the_artifact_carries_only_what_the_publish_job_validates(analysis):
    text = ANALYSIS_WORKFLOW.read_text(encoding="utf-8")

    assert "actions/upload-artifact@v7" in text
    assert "if-no-files-found: error" in text
    assert "retention-days: 1" in text
    assert 'name: crg-round' in text


# --- the skill and the scripts agree ------------------------------------


def test_the_skill_states_the_flag_policy_the_code_enforces():
    skill = unwrap(SKILL.read_text(encoding="utf-8"))

    # The prose once claimed escalation needed a finding an earlier round
    # raised while the code escalated any open high finding. Both halves of
    # that distinction are now load-bearing, so the table must name them.
    assert "A `high` finding an earlier round raised is **still open**" in skill
    assert "A `high` finding this round raised for the first time" in skill
    assert "resolve_flag" in skill
    assert "derive" in skill


def test_the_workflow_mirrors_the_reference_split():
    reference = REFERENCE.read_text(encoding="utf-8")

    # The unprivileged-then-validate shape is the repo's existing answer to
    # PR-authored content; the round workflow must not invent a second one.
    assert 'comment: "false"' in reference
    assert "workflow_run" in PUBLISH_WORKFLOW.read_text(encoding="utf-8")
