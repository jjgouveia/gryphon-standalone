"""Tests for the local re-review runner.

The agent is never invoked: ``dry_run`` short-circuits before it, and the
tests that need a subprocess stub it out. The lock and the post-round flag
check are the parts worth pinning down — both guard failures that look like
a quiet afternoon.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import review_watch  # noqa: E402
from pr_ledger import Finding, Ledger  # noqa: E402
from pr_watch import Candidate  # noqa: E402

REPO = "jjgouveia/gryphon-standalone"
HEAD = "a" * 40


def candidate(**overrides) -> Candidate:
    data = {
        "number": 29,
        "title": "some PR",
        "head": HEAD,
        "base": "staging",
        "round": 2,
        "open_findings": 1,
        "reason": "round 2: 1 open finding(s)",
        "delta_files": ("gryphon/parser.py",),
    }
    data.update(overrides)
    return Candidate(**data)


def ledger(head: str = HEAD, flag: str = "comment", findings=None) -> Ledger:
    return Ledger(
        head=head,
        base="staging",
        round=1,
        flag=flag,
        findings=findings
        or (
            Finding(id=1, status="open", sev="high", title="old blocker", files=()),
        ),
    )


def stub_thread(monkeypatch, ledger_obj: Ledger | None):
    calls: list[str] = []

    def fake(repo: str, number: int):
        calls.append(f"{repo}#{number}")
        return ledger_obj

    monkeypatch.setattr(review_watch, "newest_ledger", fake)
    return calls


# --- the prompt ----------------------------------------------------------


def test_the_prompt_names_the_pr_head_and_round():
    prompt = review_watch.build_prompt(candidate(), ROOT)

    assert "#29" in prompt
    assert HEAD[:8] in prompt
    assert "round 2" in prompt
    assert "--resume" in prompt


def test_the_prompt_tells_the_agent_to_publish_itself():
    prompt = review_watch.build_prompt(candidate(), ROOT)

    # Locally the agent has the user's credentials, so it posts the review
    # rather than handing a body to a privileged job.
    assert "Publish the review yourself" in prompt
    assert "not leave the review in a draft" in prompt


def test_the_prompt_forbids_approval():
    prompt = review_watch.build_prompt(candidate(), ROOT)

    assert "never approve" in prompt


def test_the_prompt_points_at_the_unattended_policy():
    prompt = review_watch.build_prompt(candidate(), ROOT)

    assert "Unattended rounds" in prompt


def test_the_prompt_lists_the_delta():
    prompt = review_watch.build_prompt(candidate(delta_files=("a.py", "b.py")), ROOT)

    assert "a.py, b.py" in prompt


# --- the command --------------------------------------------------------


def test_the_claude_command_is_headless():
    assert review_watch.agent_command("do it", "claude") == ["claude", "-p", "do it"]


def test_the_opencode_command_uses_run():
    assert review_watch.agent_command("do it", "opencode") == ["opencode", "run", "do it"]


# --- the lock -----------------------------------------------------------


def test_the_lock_is_exclusive(tmp_path):
    first = review_watch.Lock(tmp_path / review_watch.LOCK_NAME)

    assert first.take() is True
    assert review_watch.Lock(tmp_path / review_watch.LOCK_NAME).take() is False


def test_the_lock_records_the_owning_pid(tmp_path):
    path = tmp_path / review_watch.LOCK_NAME
    with review_watch.Lock(path):
        assert path.read_text(encoding="utf-8").strip() == str(os.getpid())


def test_the_lock_is_released_on_exit(tmp_path):
    path = tmp_path / review_watch.LOCK_NAME
    with review_watch.Lock(path):
        pass

    assert not path.exists()


def test_a_stale_lock_is_reclaimed(tmp_path, monkeypatch):
    # A scheduler that killed the process mid-round must not wedge the loop
    # for good; that failure is indistinguishable from "nothing to review".
    path = tmp_path / review_watch.LOCK_NAME
    path.write_text("999999999", encoding="utf-8")
    monkeypatch.setattr(review_watch, "pid_alive", lambda pid: False)

    lock = review_watch.Lock(path)
    assert lock.take() is True
    assert path.read_text(encoding="utf-8").strip() == str(os.getpid())


def test_a_corrupt_lock_is_reclaimed(tmp_path):
    path = tmp_path / review_watch.LOCK_NAME
    path.write_text("not a pid", encoding="utf-8")

    assert review_watch.Lock(path).take() is True


def test_pid_alive_recognises_this_process():
    assert review_watch.pid_alive(os.getpid()) is True


def test_pid_alive_rejects_nonsense():
    assert review_watch.pid_alive(0) is False
    assert review_watch.pid_alive(-1) is False


# --- the post-round check ------------------------------------------------


def test_a_published_round_is_confirmed(monkeypatch):
    after = ledger(head=HEAD, flag="comment")
    monkeypatch.setattr(review_watch, "newest_ledger", lambda repo, n: after)

    result = review_watch.check_published(REPO, candidate(), previous=None)

    assert result.status == "published"
    assert result.failed is False
    assert result.notes == []


def test_an_unexpected_escalation_is_flagged(monkeypatch):
    monkeypatch.setattr(
        review_watch, "newest_ledger", lambda repo, n: ledger(flag="request-changes")
    )

    # Nothing was carried: this high blocker is new, so the bot's first
    # disagreement with new code may not hold the merge.
    result = review_watch.check_published(REPO, candidate(), previous=None)

    assert result.expected_flag == "comment"
    assert any("policy violation" in note for note in result.notes)


def test_a_permitted_escalation_is_not_flagged(monkeypatch):
    monkeypatch.setattr(
        review_watch, "newest_ledger", lambda repo, n: ledger(flag="request-changes")
    )

    result = review_watch.check_published(REPO, candidate(), previous=ledger())

    assert result.expected_flag == "request-changes"
    assert result.notes == []


def test_a_moved_head_is_noted(monkeypatch):
    monkeypatch.setattr(
        review_watch, "newest_ledger", lambda repo, n: ledger(head="b" * 40)
    )

    result = review_watch.check_published(REPO, candidate(), previous=None)

    assert any("the next poll picks the new head up" in note for note in result.notes)
    assert result.status == "published"


def test_a_missing_ledger_after_the_round_is_unverified(monkeypatch):
    monkeypatch.setattr(review_watch, "newest_ledger", lambda repo, n: None)

    result = review_watch.check_published(REPO, candidate(), previous=None)

    assert result.status == "unverified"
    assert result.failed is True


def test_an_unreadable_thread_is_unverified(monkeypatch):
    def boom(repo, number):
        raise review_watch.GitHubError("rate limited")

    monkeypatch.setattr(review_watch, "newest_ledger", boom)

    result = review_watch.check_published(REPO, candidate(), previous=None)

    assert result.status == "unverified"
    assert "rate limited" in result.detail


# --- run_round ----------------------------------------------------------


def run_round_long(cand, repo_root, agent, dry_run, repo=REPO):
    return review_watch.run_round(
        repo, cand, repo_root=repo_root, agent=agent, dry_run=dry_run
    )


def test_dry_run_never_spawns_the_agent(monkeypatch, tmp_path):
    stub_thread(monkeypatch, ledger())
    monkeypatch.setattr(
        review_watch.subprocess, "run",
        lambda *a, **k: pytest.fail("agent was invoked"),
    )

    result = run_round_long(candidate(), tmp_path, "claude", True)

    assert result.status == "dry-run"
    assert result.failed is False


def test_dry_run_reports_the_carried_count(monkeypatch, tmp_path):
    # The flag cannot be known before the round: escalate needs the
    # verdicts the agent has not written yet. Reporting the carried count
    # is what a dry run can honestly say.
    stub_thread(monkeypatch, ledger())

    result = run_round_long(candidate(), tmp_path, "claude", True)

    assert result.carried == 1
    assert "carried from round 1" in result.detail


def test_dry_run_without_a_ledger_says_so(monkeypatch, tmp_path):
    stub_thread(monkeypatch, None)

    result = run_round_long(candidate(), tmp_path, "claude", True)

    assert result.carried == 0
    assert "no ledger" in result.detail


def test_dry_run_reports_the_previous_round_even_when_nothing_is_published(monkeypatch, tmp_path):
    # The carried set is captured before the agent runs; there is no "after"
    # to read it from, so a round that posts nothing leaves the thread as it
    # was and the next poll sees the same head again.
    seen: list[object] = []
    stub_thread(monkeypatch, ledger())

    def fake_run(cmd, **kwargs):
        return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")

    def fake_check(repo, cand, previous):
        seen.append(previous)
        return RoundResultStub()

    monkeypatch.setattr(review_watch.subprocess, "run", fake_run)
    monkeypatch.setattr(review_watch, "check_published", fake_check)

    run_round_long(candidate(), tmp_path, "claude", False)

    # check_published received the pre-round ledger, not None.
    assert seen and seen[0] is not None


def test_a_failing_agent_is_reported(monkeypatch, tmp_path):
    stub_thread(monkeypatch, ledger())

    def fake_run(cmd, **kwargs):
        return subprocess.CompletedProcess(cmd, 1, stdout="", stderr="boom\nstack\n")

    monkeypatch.setattr(review_watch.subprocess, "run", fake_run)

    result = run_round_long(candidate(), tmp_path, "claude", False)

    assert result.status == "agent-failed"
    assert result.failed is True
    assert "stack" in result.detail


def test_the_agent_runs_in_the_checkout(monkeypatch, tmp_path):
    stub_thread(monkeypatch, ledger())
    seen: dict[str, object] = {}

    class Ok:
        returncode = 0
        stdout = ""
        stderr = ""

    def fake_run(cmd, **kwargs):
        seen.update(kwargs)
        return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")

    monkeypatch.setattr(review_watch.subprocess, "run", fake_run)
    monkeypatch.setattr(review_watch, "check_published", lambda repo, cand, exp: RoundResultStub())

    run_round_long(candidate(), tmp_path, "claude", False)

    assert seen["cwd"] == tmp_path


class RoundResultStub:
    status = "published"
    failed = False


# --- CLI -----------------------------------------------------------------


def run_cli(*args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, str(ROOT / "scripts" / "review_watch.py"), *args],
        capture_output=True,
        text=True,
    )


def test_cli_requires_a_repo():
    assert run_cli().returncode == 2


def test_cli_refuses_a_directory_that_is_not_the_checkout(tmp_path):
    result = run_cli("--repo", REPO, "--repo-root", str(tmp_path))

    assert result.returncode == 2
    assert "does not look like the gryphon checkout" in result.stderr


def test_cli_polls_the_real_repo_dry():
    result = run_cli("--repo", REPO, "--repo-root", str(ROOT), "--dry-run")

    # The real repo has no labeled PR, so this is the quiet path. It proves
    # the gh plumbing and the checkout guard without asserting a PR state.
    assert result.returncode == 0, result.stderr


def test_the_skill_documents_the_local_runner():
    skill = (ROOT / "skills" / "review-pr" / "SKILL.md").read_text(encoding="utf-8")

    assert "scripts/review_watch.py" in skill
    assert "--register" in skill
