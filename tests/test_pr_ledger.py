"""Contract tests for the ``review-pr`` state ledger."""

from __future__ import annotations

import json
import re
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import pr_ledger  # noqa: E402
from pr_ledger import (  # noqa: E402
    Finding,
    Ledger,
    LedgerError,
    decide,
    extract,
    latest,
    render,
    touches_open_findings,
)

HEAD = "a" * 40
OTHER_HEAD = "b" * 40
SKILL = ROOT / "skills" / "review-pr" / "SKILL.md"


def make_ledger(**overrides) -> Ledger:
    kwargs = {
        "head": HEAD,
        "base": "staging",
        "round": 1,
        "flag": "comment",
        "findings": (
            Finding(id=1, status="open", sev="high", title="boom", files=("a.py",)),
            Finding(id=2, status="resolved", sev="low", title="nit", files=()),
        ),
    }
    kwargs.update(overrides)
    return Ledger(**kwargs)


def body_with(text: str) -> str:
    return f"## segunda passada\n\nResolved #1, see below.\n\n{text}\n"


# --- rendering -----------------------------------------------------------


def test_render_is_one_line_and_ends_with_the_comment_terminator():
    rendered = render(make_ledger())

    assert "\n" not in rendered
    assert rendered.startswith(pr_ledger.MARKER)
    assert rendered.endswith(" -->")


def test_render_round_trips_through_parse():
    ledger = make_ledger()

    assert extract(render(ledger)) == ledger


def test_render_escapes_the_characters_that_would_close_the_comment():
    hostile = make_ledger(
        findings=(
            Finding(
                id=1,
                status="open",
                sev="high",
                title="compares a < b and c > d & --> closes early",
                files=(),
            ),
        )
    )

    rendered = render(hostile)

    # Nothing that could terminate the comment survives unescaped.
    body = body_with(rendered)
    assert body.count("-->") == 1
    assert extract(body) == hostile
    assert hostile.findings[0].title == "compares a < b and c > d & --> closes early"


def test_escaping_does_not_touch_json_structure():
    rendered = render(make_ledger())

    payload = re.sub(r"\s*-->$", "", rendered[len(pr_ledger.MARKER) :])
    assert isinstance(json.loads(payload), dict)


# --- parsing -------------------------------------------------------------


def test_extract_returns_none_without_a_block():
    assert extract("just a review body") is None
    assert extract(None) is None
    assert extract("") is None


def test_extract_ignores_a_second_comment_after_the_ledger():
    body = body_with(render(make_ledger())) + "\n<!-- unrelated -->\n"

    assert extract(body) == make_ledger()


def test_the_last_ledger_wins_over_one_quoted_in_the_prose():
    # An agent resuming reads the previous review body and may quote it. A
    # stale copy in the prose must not become the resume point, or every
    # later round pins itself to a head it already reviewed.
    stale = make_ledger(round=1, head="a" * 40)
    current = make_ledger(round=2, head="b" * 40)

    body = f"## rodada 2\n\nProse quoting round 1:\n\n{render(stale)}\n\n{render(current)}\n"

    assert extract(body) == current


def test_a_body_with_only_a_stale_ledger_still_reads():
    assert extract(body_with(render(make_ledger()))) == make_ledger()


def test_latest_takes_the_first_ledger_given_newest_first():
    first = render(make_ledger(round=2))
    second = render(make_ledger(round=1))

    assert latest([first, second, None]) == make_ledger(round=2)
    assert latest([None, None]) is None


@pytest.mark.parametrize(
    "field,value",
    [
        ("v", 2),
        ("v", "1"),
        ("head", "abc123"),
        ("head", "A" * 40),
        ("round", 0),
        ("round", "2"),
        ("flag", "merge"),
        ("flag", ""),
        ("base", ""),
    ],
)
def test_parse_rejects_invalid_top_level_fields(field, value):
    payload = {
        "v": 1,
        "head": HEAD,
        "base": "staging",
        "round": 1,
        "flag": "comment",
        "findings": [],
    }
    payload[field] = value

    with pytest.raises(LedgerError):
        pr_ledger.parse_block(json.dumps(payload))


@pytest.mark.parametrize(
    "finding",
    [
        {"id": 0, "status": "open", "sev": "high", "title": "x"},
        {"id": "1", "status": "open", "sev": "high", "title": "x"},
        {"id": 1, "status": "maybe", "sev": "high", "title": "x"},
        {"id": 1, "status": "open", "sev": "critical", "title": "x"},
        {"id": 1, "status": "open", "sev": "high"},
        {"id": 1, "status": "open", "sev": "high", "title": "x", "files": "a.py"},
        {"id": 1, "status": "open", "sev": "high", "title": "x", "files": [7]},
        {"id": True, "status": "open", "sev": "high", "title": "x"},
        "not-an-object",
    ],
)
def test_parse_rejects_invalid_findings(finding):
    payload = {
        "v": 1,
        "head": HEAD,
        "base": "staging",
        "round": 1,
        "flag": "comment",
        "findings": [finding],
    }

    with pytest.raises(LedgerError):
        pr_ledger.parse_block(json.dumps(payload))


def test_parse_rejects_duplicate_finding_ids():
    finding = {"id": 1, "status": "open", "sev": "high", "title": "x"}
    payload = {
        "v": 1,
        "head": HEAD,
        "base": "staging",
        "round": 1,
        "flag": "comment",
        "findings": [finding, finding],
    }

    with pytest.raises(LedgerError, match="duplicate"):
        pr_ledger.parse_block(json.dumps(payload))


def test_a_truncated_ledger_raises_rather_than_reading_as_absent():
    body = '<!-- gryphon-review-state {"v":1,"head":"' + HEAD + '","round":1}} -->'

    # Raising matters: a corrupt block must not degrade to None, because
    # None means "round 1", which restarts numbering and re-reports every
    # finding a previous round already settled.
    with pytest.raises(LedgerError):
        extract(body)


# --- ledger helpers ------------------------------------------------------


def test_open_files_collects_only_open_findings():
    ledger = make_ledger(
        findings=(
            Finding(id=1, status="open", sev="high", title="a", files=("a.py", "b.py")),
            Finding(id=2, status="resolved", sev="high", title="b", files=("c.py",)),
            Finding(id=3, status="obsolete", sev="medium", title="c", files=("d.py",)),
        )
    )

    assert ledger.open_files == frozenset({"a.py", "b.py"})
    assert [f.id for f in ledger.open_findings] == [1]


def test_next_id_never_reuses_an_id():
    ledger = make_ledger(
        findings=(
            Finding(id=4, status="resolved", sev="low", title="a"),
            Finding(id=9, status="open", sev="low", title="b"),
        )
    )

    assert ledger.next_id() == 10


def test_next_id_starts_at_one():
    assert Ledger(head=HEAD, base="main", round=1, flag="comment").next_id() == 1


# --- the resume decision table ------------------------------------------


def test_no_ledger_is_a_first_pass():
    decision, reason = decide(None, current_head=HEAD, current_base="staging")

    assert decision == "first-pass"
    assert "round 1" in reason


def test_retargeted_base_invalidates_every_finding():
    decision, reason = decide(
        make_ledger(), current_head=OTHER_HEAD, current_base="main"
    )

    assert decision == "retargeted"
    assert "staging" in reason and "main" in reason


def test_same_head_is_never_reviewed_twice():
    decision, reason = decide(
        make_ledger(), current_head=HEAD, current_base="staging"
    )

    assert decision == "already-reviewed"
    assert str(1) in reason


def test_a_new_head_resumes_at_the_next_round():
    decision, reason = decide(
        make_ledger(round=3), current_head=OTHER_HEAD, current_base="staging"
    )

    assert decision == "resume"
    assert "round 4" in reason
    assert "1 open finding" in reason


def test_round_cap_stops_the_loop():
    decision, reason = decide(
        make_ledger(round=6),
        current_head=OTHER_HEAD,
        current_base="staging",
        max_rounds=6,
    )

    assert decision == "rounds-exhausted"
    assert "6" in reason


def test_round_cap_does_not_stop_below_it():
    decision, _ = decide(
        make_ledger(round=5),
        current_head=OTHER_HEAD,
        current_base="staging",
        max_rounds=6,
    )

    assert decision == "resume"


def test_retarget_wins_over_the_round_cap():
    decision, _ = decide(
        make_ledger(round=9),
        current_head=OTHER_HEAD,
        current_base="main",
        max_rounds=6,
    )

    assert decision == "retargeted"


# --- the cheap delta filter ---------------------------------------------


def test_delta_touching_an_open_finding_is_relevant():
    ledger = make_ledger()

    assert touches_open_findings(ledger, ["a.py"]) is True
    assert touches_open_findings(ledger, ["unrelated.py", "a.py"]) is True


def test_delta_avoiding_every_open_finding_is_skipped():
    ledger = make_ledger()

    assert touches_open_findings(ledger, ["c.py", "unrelated.py"]) is False


def test_a_resolved_finding_cannot_be_relevant():
    ledger = make_ledger(
        findings=(Finding(id=2, status="resolved", sev="high", title="b", files=("c.py",)),)
    )

    assert touches_open_findings(ledger, ["c.py"]) is False


def test_an_unknown_delta_is_treated_as_relevant():
    ledger = make_ledger()

    assert touches_open_findings(ledger, []) is True


def test_no_open_findings_means_nothing_to_verify():
    ledger = make_ledger(
        findings=(Finding(id=2, status="resolved", sev="high", title="b", files=("c.py",)),)
    )

    assert touches_open_findings(ledger, ["c.py"]) is False


# --- the skill documents what the module enforces -----------------------


def test_skill_documents_the_marker_and_the_escape_rule():
    skill = SKILL.read_text(encoding="utf-8")

    assert "## State ledger" in skill
    assert "gryphon-review-state" in skill
    assert "U+003C, U+003E" in skill
    assert "U+0026" in skill
    assert "--resume" in skill


def test_skill_defers_to_the_module_instead_of_redoing_the_rules():
    skill = SKILL.read_text(encoding="utf-8")

    assert "scripts/pr_ledger.py" in skill
    assert "decide(" in skill
    assert "touches_open_findings(" in skill
    assert "LedgerError" in skill


def test_skill_states_the_unattended_flag_policy():
    skill = SKILL.read_text(encoding="utf-8")

    assert "## Unattended rounds" in skill
    assert "request-changes" in skill
    assert "never** `approve`" in skill


def test_skill_documents_the_ledger_growth_constraint():
    skill = SKILL.read_text(encoding="utf-8")

    assert "## Ledger growth" in skill
    assert "identity" in skill


def test_skill_documents_the_watch_entry_point_and_its_traps():
    skill = SKILL.read_text(encoding="utf-8")

    assert "## Watching a PR" in skill
    assert "scripts/pr_watch.py" in skill
    # The trigger is the published review, so nothing is registered by hand.
    assert "no label to apply" in skill
    assert "reviewed-by" in skill


def test_skill_example_ledger_parses():
    skill = SKILL.read_text(encoding="utf-8")
    body = re.search(r"```html\n(.*?)\n```", skill, re.DOTALL)

    assert body is not None, "skill must show the ledger in a fenced html block"
    ledger = extract(body.group(1))
    assert ledger is not None
    assert ledger.round == 3
    assert ledger.flag == "request-changes"
    assert ledger.findings[0].id == 3


# --- CLI -----------------------------------------------------------------


def run_cli(*args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, str(ROOT / "scripts" / "pr_ledger.py"), *args],
        capture_output=True,
        text=True,
    )


def test_cli_prints_the_decision(tmp_path):
    body = tmp_path / "review.md"
    body.write_text(body_with(render(make_ledger(round=2))), encoding="utf-8")

    result = run_cli(str(body), "--current-head", OTHER_HEAD, "--current-base", "staging")

    assert result.returncode == 0
    assert result.stdout.startswith("resume: round 3")


def test_cli_fails_on_a_missing_ledger(tmp_path):
    body = tmp_path / "review.md"
    body.write_text("no ledger here", encoding="utf-8")

    assert run_cli(str(body)).returncode == 3


def test_cli_treats_a_missing_ledger_as_ok_when_optional(tmp_path):
    body = tmp_path / "review.md"
    body.write_text("no ledger here", encoding="utf-8")

    assert run_cli(str(body), "--optional").returncode == 0


def test_cli_fails_on_an_invalid_ledger(tmp_path):
    body = tmp_path / "review.md"
    body.write_text('<!-- gryphon-review-state {"v":99} -->', encoding="utf-8")

    assert run_cli(str(body)).returncode == 3


def test_cli_fails_on_an_unreadable_file(tmp_path):
    assert run_cli(str(tmp_path / "missing.md")).returncode == 2
