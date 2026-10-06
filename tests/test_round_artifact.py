"""Tests for the unattended round body's composition.

The privileged workflow posts whatever this script renders, so the checks
here are the last line of defence before agent prose reaches a PR thread.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import round_artifact  # noqa: E402
from pr_ledger import Finding, Ledger, extract, render  # noqa: E402

HEAD = "a" * 40

VERDICTS = {
    "flag": "comment",
    "round": 2,
    "findings": [
        {
            "id": 1,
            "status": "resolved",
            "sev": "high",
            "title": "old",
            "files": ["a.py"],
        },
        {"id": 2, "status": "open", "sev": "low", "title": "nit", "files": []},
    ],
}


def write(
    tmp_path: Path,
    verdicts: object,
    prose: str = "## rodada 2\n\nVeredito.",
) -> tuple[Path, Path]:
    prose_file = tmp_path / "prose.md"
    prose_file.write_text(prose, encoding="utf-8")
    verdicts_file = tmp_path / "verdicts.json"
    verdicts_file.write_text(
        verdicts if isinstance(verdicts, str) else json.dumps(verdicts),
        encoding="utf-8",
    )
    return prose_file, verdicts_file


# --- the verdicts contract ----------------------------------------------


def test_valid_verdicts_are_accepted():
    flag, findings, round_number = round_artifact.parse_verdicts(json.dumps(VERDICTS))

    assert flag == "comment"
    assert round_number == 2
    assert [f.id for f in findings] == [1, 2]


def test_approve_is_refused_by_the_unattended_policy():
    payload = dict(VERDICTS, flag="approve")

    with pytest.raises(round_artifact.VerdictError, match="never approve"):
        round_artifact.parse_verdicts(json.dumps(payload))


@pytest.mark.parametrize(
    "payload",
    [
        {"flag": "comment", "round": 2},
        {"flag": "comment", "round": 0, "findings": []},
        {"flag": "comment", "round": "2", "findings": []},
        {"flag": "comment", "round": 2, "findings": "nope"},
        {"flag": "comment", "round": 2, "findings": [{"id": 1}]},
        {"flag": "comment", "round": 2, "findings": ["x"]},
    ],
)
def test_malformed_verdicts_are_rejected(payload):
    with pytest.raises(round_artifact.VerdictError):
        round_artifact.parse_verdicts(json.dumps(payload))


def test_duplicate_finding_ids_are_rejected():
    finding = VERDICTS["findings"][0]
    payload = dict(VERDICTS, findings=[finding, finding])

    with pytest.raises(round_artifact.VerdictError, match="duplicate"):
        round_artifact.parse_verdicts(json.dumps(payload))


def test_an_invalid_finding_status_is_rejected_by_the_ledger_schema():
    bad = dict(VERDICTS["findings"][0], status="maybe")
    payload = dict(VERDICTS, findings=[bad])

    with pytest.raises(Exception):
        round_artifact.parse_verdicts(json.dumps(payload))


def test_verdicts_that_are_not_json_are_rejected():
    with pytest.raises(round_artifact.VerdictError, match="not valid JSON"):
        round_artifact.parse_verdicts("{oops")


def test_resolve_flag_downgrades_an_unsupported_escalation():
    previous = Ledger(
        head=HEAD,
        base="staging",
        round=1,
        flag="comment",
        findings=(
            Finding(id=1, status="open", sev="low", title="nit", files=[]),
        ),
    )
    fresh_high = (Finding(id=9, status="open", sev="high", title="new", files=[]),)

    assert round_artifact.resolve_flag("request-changes", fresh_high, previous) == "comment"


def test_resolve_flag_keeps_a_disconfirmed_escalation():
    previous = Ledger(
        head=HEAD,
        base="staging",
        round=1,
        flag="request-changes",
        findings=(Finding(id=1, status="open", sev="high", title="old", files=[]),),
    )
    verdicts = (Finding(id=1, status="open", sev="high", title="old", files=[]),)

    assert round_artifact.resolve_flag("request-changes", verdicts, previous) == "request-changes"


def test_resolve_flag_without_a_previous_ledger_only_comments():
    verdicts = (Finding(id=1, status="open", sev="high", title="x", files=[]),)

    assert round_artifact.resolve_flag("request-changes", verdicts, None) == "comment"


# --- the composed body ---------------------------------------------------


def test_composed_body_carries_prose_and_a_ledger():
    flag, findings, round_number = round_artifact.parse_verdicts(json.dumps(VERDICTS))

    body = round_artifact.compose(
        "## rodada 2\n\nVeredito.",
        findings,
        head=HEAD,
        base="staging",
        round_number=round_number,
        flag=flag,
    )

    assert body.startswith("## rodada 2")
    ledger = extract(body)
    assert ledger is not None
    assert ledger.head == HEAD
    assert ledger.round == 2
    assert [f.id for f in ledger.findings] == [1, 2]


def test_a_hostile_finding_title_cannot_break_the_ledger():
    payload = dict(
        VERDICTS,
        findings=[
            {
                "id": 1,
                "status": "open",
                "sev": "high",
                "title": "a < b & c > d --> closing early",
                "files": [],
            }
        ],
    )
    flag, findings, round_number = round_artifact.parse_verdicts(json.dumps(payload))

    body = round_artifact.compose(
        "prosa", findings, head=HEAD, base="staging", round_number=round_number, flag=flag
    )

    # The prose summary legitimately shows the title as written; what must
    # hold is that the escaped JSON inside the comment parses, and that the
    # comment is not terminated early by the raw arrow in the summary.
    ledger = extract(body)
    assert ledger is not None
    assert ledger.findings[0].title == "a < b & c > d --> closing early"
    ledger_line = body.split("<!-- gryphon-review-state", 1)[1]
    assert ledger_line.count("-->") == 1
    assert "--\\u003e" in ledger_line


def test_request_changes_survives_into_the_ledger():
    payload = dict(VERDICTS, flag="request-changes")
    flag, findings, round_number = round_artifact.parse_verdicts(json.dumps(payload))

    body = round_artifact.compose(
        "prosa", findings, head=HEAD, base="staging", round_number=round_number, flag=flag
    )

    assert extract(body).flag == "request-changes"


# --- the CLI the workflow calls ----------------------------------------


def run_cli(*args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, str(ROOT / "scripts" / "round_artifact.py"), *args],
        capture_output=True,
        text=True,
    )


def test_cli_writes_the_body_and_metadata(tmp_path):
    prose, verdicts = write(tmp_path, VERDICTS)
    out = tmp_path / "round"

    result = run_cli(
        "--prose", str(prose), "--verdicts", str(verdicts),
        "--pr", "27", "--head", HEAD, "--base", "staging", "--out", str(out),
    )

    assert result.returncode == 0, result.stderr
    meta = json.loads((out / "meta.json").read_text(encoding="utf-8"))
    assert meta == {
        "pr_number": 27,
        "head": HEAD,
        "base": "staging",
        "round": 2,
        "flag": "comment",
        "open_findings": 1,
    }
    body = (out / "body.md").read_text(encoding="utf-8")
    assert extract(body).head == HEAD


def test_cli_writes_the_body_without_a_bom(tmp_path):
    prose, verdicts = write(tmp_path, VERDICTS)
    out = tmp_path / "round"
    run_cli(
        "--prose", str(prose), "--verdicts", str(verdicts),
        "--pr", "27", "--head", HEAD, "--base", "staging", "--out", str(out),
    )

    assert not (out / "body.md").read_bytes().startswith(b"\xef\xbb\xbf")


def test_cli_refuses_an_approving_round(tmp_path):
    prose, verdicts = write(tmp_path, dict(VERDICTS, flag="approve"))
    out = tmp_path / "round"

    result = run_cli(
        "--prose", str(prose), "--verdicts", str(verdicts),
        "--pr", "27", "--head", HEAD, "--base", "staging", "--out", str(out),
    )

    assert result.returncode == 3
    assert not (out / "body.md").exists()


def test_cli_rejects_empty_prose(tmp_path):
    prose, verdicts = write(tmp_path, VERDICTS, prose="   \n")
    out = tmp_path / "round"

    result = run_cli(
        "--prose", str(prose), "--verdicts", str(verdicts),
        "--pr", "27", "--head", HEAD, "--base", "staging", "--out", str(out),
    )

    assert result.returncode == 3


def test_cli_rejects_oversized_prose(tmp_path):
    prose, verdicts = write(tmp_path, VERDICTS, prose="x" * (round_artifact.PROSE_LIMIT + 1))
    out = tmp_path / "round"

    result = run_cli(
        "--prose", str(prose), "--verdicts", str(verdicts),
        "--pr", "27", "--head", HEAD, "--base", "staging", "--out", str(out),
    )

    assert result.returncode == 3


def test_cli_rejects_prose_that_quotes_a_ledger(tmp_path):
    # The agent resumes by reading the previous review body. Quoting it
    # verbatim puts a second ledger in the body, which is a contract
    # violation worth failing on rather than publishing.
    prose, verdicts = write(tmp_path, VERDICTS, prose="## rodada 2\n\n" + previous_body())
    out = tmp_path / "round"

    result = run_cli(
        "--prose", str(prose), "--verdicts", str(verdicts),
        "--pr", "27", "--head", HEAD, "--base", "staging", "--out", str(out),
    )

    assert result.returncode == 3
    assert not (out / "body.md").exists()


def test_cli_reports_a_missing_input(tmp_path):
    out = tmp_path / "round"

    result = run_cli(
        "--prose", str(tmp_path / "nope.md"), "--verdicts", str(tmp_path / "nope.json"),
        "--pr", "27", "--head", HEAD, "--base", "staging", "--out", str(out),
    )

    assert result.returncode == 2


def previous_body(flag: str = "request-changes") -> str:
    return "## rodada 1\n\n" + render(
        Ledger(
            head="c" * 40,
            base="staging",
            round=1,
            flag=flag,
            findings=(
                Finding(id=1, status="open", sev="high", title="old blocker", files=[]),
            ),
        )
    )


def test_cli_escalates_when_the_prior_ledger_shows_an_unfixed_blocker(tmp_path):
    # The prior round raised #1 as a high blocker; this round leaves it open.
    unfixed = dict(
        VERDICTS,
        flag="request-changes",
        findings=[
            {"id": 1, "status": "open", "sev": "high", "title": "old blocker", "files": []},
            {"id": 2, "status": "open", "sev": "low", "title": "nit", "files": []},
        ],
    )
    prose, verdicts = write(tmp_path, unfixed)
    previous = tmp_path / "prev.md"
    previous.write_text(previous_body(), encoding="utf-8")
    out = tmp_path / "round"

    result = run_cli(
        "--prose", str(prose), "--verdicts", str(verdicts),
        "--pr", "27", "--head", HEAD, "--base", "staging",
        "--previous-ledger", str(previous), "--out", str(out),
    )

    assert result.returncode == 0, result.stderr
    meta = json.loads((out / "meta.json").read_text(encoding="utf-8"))
    assert meta["flag"] == "request-changes"


def test_cli_downgrades_when_the_blocker_was_never_raised_before(tmp_path):
    fresh_high = dict(
        VERDICTS,
        flag="request-changes",
        findings=[
            {"id": 9, "status": "open", "sev": "high", "title": "brand new", "files": []},
        ],
    )
    prose, verdicts = write(tmp_path, fresh_high)
    previous = tmp_path / "prev.md"
    previous.write_text(previous_body(flag="comment"), encoding="utf-8")
    out = tmp_path / "round"

    result = run_cli(
        "--prose", str(prose), "--verdicts", str(verdicts),
        "--pr", "27", "--head", HEAD, "--base", "staging",
        "--previous-ledger", str(previous), "--out", str(out),
    )

    assert result.returncode == 0, result.stderr
    meta = json.loads((out / "meta.json").read_text(encoding="utf-8"))
    assert meta["flag"] == "comment"
    # The ledger the next round reads must agree with what was published.
    body = (out / "body.md").read_text(encoding="utf-8")
    assert extract(body).flag == "comment"


def test_cli_composes_without_a_previous_ledger_rather_than_failing(tmp_path):
    # No repo and no prior body: the round still publishes, as a comment.
    prose, verdicts = write(tmp_path, dict(VERDICTS, flag="request-changes"))
    out = tmp_path / "round"

    result = run_cli(
        "--prose", str(prose), "--verdicts", str(verdicts),
        "--pr", "27", "--head", HEAD, "--base", "staging", "--out", str(out),
    )

    assert result.returncode == 0, result.stderr
    assert json.loads((out / "meta.json").read_text(encoding="utf-8"))["flag"] == "comment"


def test_cli_survives_an_unreadable_previous_ledger(tmp_path):
    prose, verdicts = write(tmp_path, dict(VERDICTS, flag="request-changes"))
    previous = tmp_path / "prev.md"
    previous.write_text('<!-- gryphon-review-state {"v":99} -->', encoding="utf-8")
    out = tmp_path / "round"

    result = run_cli(
        "--prose", str(prose), "--verdicts", str(verdicts),
        "--pr", "27", "--head", HEAD, "--base", "staging",
        "--previous-ledger", str(previous), "--out", str(out),
    )

    assert result.returncode == 0, result.stderr
    assert json.loads((out / "meta.json").read_text(encoding="utf-8"))["flag"] == "comment"


def test_cli_reports_an_unreadable_ledger_before_publishing(tmp_path):
    # A body that cannot be read back would end the loop at the next round,
    # so the failure must happen here, not after the post.
    prose, verdicts = write(tmp_path, VERDICTS)
    out = tmp_path / "round"

    result = run_cli(
        "--prose", str(prose), "--verdicts", str(verdicts),
        "--pr", "27", "--head", "not-a-sha", "--base", "staging", "--out", str(out),
    )

    assert result.returncode == 4
    assert not (out / "body.md").exists()
