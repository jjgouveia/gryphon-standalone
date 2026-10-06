"""Tests for the interactive publish path.

The point of this script is that a review cannot be published without a
ledger, so the tests are mostly about what it refuses to do.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import publish_review  # noqa: E402
from pr_ledger import Finding, Ledger, extract, render  # noqa: E402

REPO = "Ativos-Tecnologia/cvld"
HEAD = "a" * 40

FINDINGS = [
    {
        "id": 1,
        "status": "open",
        "sev": "high",
        "title": "railpack.toml nao e lido",
        "files": ["railpack.toml", "railpack.json"],
    }
]


def write_inputs(tmp_path: Path, prose: str = "## Revisão\n\nBloqueador.", findings=FINDINGS):
    prose_file = tmp_path / "prose.md"
    prose_file.write_text(prose, encoding="utf-8")
    verdicts_file = tmp_path / "verdicts.json"
    verdicts_file.write_text(json.dumps({"findings": findings}), encoding="utf-8")
    return prose_file, verdicts_file


def run_cli(*args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, str(ROOT / "scripts" / "publish_review.py"), *args],
        capture_output=True,
        text=True,
    )


def base_args(prose: Path, verdicts: Path | None = None) -> list[str]:
    args = [
        "--repo", REPO, "--pr", "2145", "--flag", "request-changes",
        "--prose", str(prose), "--head", HEAD, "--base", "main", "--round", "1",
    ]
    if verdicts is not None:
        args += ["--verdicts", str(verdicts)]
    return args


# --- composing ----------------------------------------------------------


def test_the_composed_body_carries_a_ledger():
    body = publish_review.compose(
        "## Revisão\n\nBloqueador.",
        (Finding(id=1, status="open", sev="high", title="x", files=("railpack.toml",)),),
        head=HEAD,
        base="main",
        round_number=1,
        flag="request-changes",
    )

    ledger = extract(body)
    assert ledger is not None
    assert ledger.head == HEAD
    assert ledger.flag == "request-changes"
    assert ledger.round == 1


def test_verify_rejects_a_body_with_no_ledger():
    with pytest.raises(Exception, match="does not carry"):
        publish_review.verify("## Revisão\n\nSem ledger.", head=HEAD, flag="comment")


def test_verify_rejects_a_mismatched_head():
    body = publish_review.compose(
        "prosa", (), head="b" * 40, base="main", round_number=1, flag="comment"
    )

    with pytest.raises(Exception, match="records head"):
        publish_review.verify(body, head=HEAD, flag="comment")


def test_verify_rejects_a_mismatched_flag():
    body = publish_review.compose(
        "prosa", (), head=HEAD, base="main", round_number=1, flag="comment"
    )

    with pytest.raises(Exception, match="records flag"):
        publish_review.verify(body, head=HEAD, flag="request-changes")


def test_verify_accepts_what_compose_produced():
    body = publish_review.compose(
        "prosa", (), head=HEAD, base="main", round_number=2, flag="comment"
    )

    publish_review.verify(body, head=HEAD, flag="comment")


# --- the round number ---------------------------------------------------


def test_the_round_continues_the_thread(monkeypatch):
    monkeypatch.setattr(
        publish_review,
        "newest_ledger",
        lambda repo, pr: Ledger(
            head="b" * 40, base="main", round=3, flag="comment", findings=()
        ),
    )

    assert publish_review.next_round(REPO, 2145) == 4


def test_a_thread_with_no_ledger_starts_at_round_one(monkeypatch):
    monkeypatch.setattr(publish_review, "newest_ledger", lambda repo, pr: None)

    assert publish_review.next_round(REPO, 2145) == 1


def test_an_unreadable_thread_does_not_restart_the_numbering(monkeypatch):
    # Falling back to round 1 on a read failure would re-open every finding
    # a previous round settled, so the failure is reported instead.
    def boom(repo, pr):
        raise publish_review.GitHubError("rate limited")

    monkeypatch.setattr(publish_review, "newest_ledger", boom)

    assert publish_review.next_round(REPO, 2145) == 1


# --- the CLI refuses ----------------------------------------------------


def test_cli_dry_run_composes_without_publishing(tmp_path):
    prose, verdicts = write_inputs(tmp_path)

    result = run_cli(*base_args(prose, verdicts), "--dry-run")

    assert result.returncode == 0, result.stderr
    assert extract(result.stdout) is not None


def test_cli_refuses_prose_that_quotes_a_ledger(tmp_path):
    body = render(
        Ledger(head="b" * 40, base="main", round=1, flag="comment", findings=())
    )
    prose, verdicts = write_inputs(tmp_path, prose=f"## Revisão\n\n{body}\n")

    result = run_cli(*base_args(prose, verdicts), "--dry-run")

    assert result.returncode == 3
    assert "the ledger is composed here" in result.stderr


def test_cli_refuses_empty_prose(tmp_path):
    prose, verdicts = write_inputs(tmp_path, prose="   \n")

    assert run_cli(*base_args(prose, verdicts), "--dry-run").returncode == 3


def test_cli_refuses_an_invalid_finding(tmp_path):
    prose, verdicts = write_inputs(
        tmp_path, findings=[{"id": 1, "status": "maybe", "sev": "high", "title": "x"}]
    )

    assert run_cli(*base_args(prose, verdicts), "--dry-run").returncode == 3


def test_cli_refuses_an_unknown_flag(tmp_path):
    prose, _ = write_inputs(tmp_path)

    result = run_cli(
        "--repo", REPO, "--pr", "2145", "--flag", "merge",
        "--prose", str(prose), "--head", HEAD, "--base", "main",
    )

    assert result.returncode == 2


def test_cli_reports_a_missing_prose(tmp_path):
    result = run_cli(
        "--repo", REPO, "--pr", "2145", "--flag", "comment",
        "--prose", str(tmp_path / "nope.md"), "--head", HEAD, "--base", "main",
    )

    assert result.returncode == 2


def test_cli_accepts_a_review_with_no_findings(tmp_path):
    prose, _ = write_inputs(tmp_path)

    result = run_cli(*base_args(prose), "--dry-run")

    assert result.returncode == 0
    assert extract(result.stdout).findings == ()


def test_cli_accepts_a_bare_findings_list(tmp_path):
    # Some callers write just the list rather than an object with a
    # findings key; both are the same thing.
    prose_file = tmp_path / "prose.md"
    prose_file.write_text("prosa", encoding="utf-8")
    verdicts = tmp_path / "verdicts.json"
    verdicts.write_text(json.dumps(FINDINGS), encoding="utf-8")

    assert run_cli(*base_args(prose_file, verdicts), "--dry-run").returncode == 0


def test_cli_tolerates_a_bom_on_the_inputs(tmp_path):
    # An editor that adds a BOM must not turn into a JSON parse error or a
    # stray character in the published review.
    prose, verdicts = write_inputs(tmp_path)
    prose.write_bytes(b"\xef\xbb\xbf" + prose.read_bytes())
    verdicts.write_bytes(b"\xef\xbb\xbf" + verdicts.read_bytes())

    result = run_cli(*base_args(prose, verdicts), "--dry-run")

    assert result.returncode == 0, result.stderr
    assert not result.stdout.startswith("\ufeff")


# --- publishing ---------------------------------------------------------


def test_publish_uses_the_review_command(monkeypatch, tmp_path):
    seen: list[list[str]] = []

    def fake_run(cmd, **kwargs):
        seen.append(cmd)
        return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")

    monkeypatch.setattr(publish_review.subprocess, "run", fake_run)
    body = tmp_path / "body.md"
    body.write_text("x", encoding="utf-8")

    publish_review.publish(REPO, 2145, "request-changes", body)

    assert seen[0][:3] == ["gh", "pr", "review"]
    assert "--request-changes" in seen[0]


def test_publish_falls_back_to_a_comment_on_a_closed_pr(monkeypatch, tmp_path):
    seen: list[list[str]] = []

    def fake_run(cmd, **kwargs):
        seen.append(cmd)
        return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")

    monkeypatch.setattr(publish_review.subprocess, "run", fake_run)
    body = tmp_path / "body.md"
    body.write_text("x", encoding="utf-8")

    publish_review.publish(REPO, 2145, "request-changes", body, closed=True)

    assert seen[0][:3] == ["gh", "pr", "comment"]
    assert "--request-changes" not in seen[0]


def test_a_gh_failure_is_reported(monkeypatch, tmp_path):
    def fake_run(cmd, **kwargs):
        return subprocess.CompletedProcess(cmd, 1, stdout="", stderr="nope")

    monkeypatch.setattr(publish_review.subprocess, "run", fake_run)
    body = tmp_path / "body.md"
    body.write_text("x", encoding="utf-8")

    with pytest.raises(publish_review.GitHubError, match="nope"):
        publish_review.publish(REPO, 2145, "comment", body)


def test_pr_is_open_reads_the_state(monkeypatch):
    monkeypatch.setattr(
        publish_review.subprocess,
        "run",
        lambda cmd, **k: subprocess.CompletedProcess(
            cmd, 0, stdout=json.dumps({"state": "MERGED"}), stderr=""
        ),
    )

    assert publish_review.pr_is_open(REPO, 2145) is False


# --- the skill mandates it ---------------------------------------------


def test_the_skill_publishes_through_the_script():
    skill = (ROOT / "skills" / "review-pr" / "SKILL.md").read_text(encoding="utf-8")

    assert "publish_review.py" in skill
    # The raw gh command must not be the documented way to publish any more.
    assert "gh pr review <n> --repo <owner>/<repo> --<flag> --body-file <file>" not in skill
