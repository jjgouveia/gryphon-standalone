"""Tests for the re-review watcher's poll logic.

``gh`` is stubbed so the decision path is exercised without a network
call; one test at the end hits the real API and is skipped unless the
watch label exists.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import pr_watch  # noqa: E402
from pr_ledger import Finding, Ledger, extract, render  # noqa: E402
from pr_watch import (  # noqa: E402
    Candidate,
    GitHubError,
    evaluate,
    fetch_review_bodies,
    survey,
)

REPO = "jjgouveia/gryphon-standalone"
HEAD = "a" * 40
NEXT_HEAD = "b" * 40


def make_ledger(**overrides) -> Ledger:
    kwargs = {
        "head": HEAD,
        "base": "staging",
        "round": 1,
        "flag": "comment",
        "findings": (
            Finding(id=1, status="open", sev="high", title="boom", files=("gryphon/parser.py",)),
        ),
    }
    kwargs.update(overrides)
    return Ledger(**kwargs)


def pull(**overrides) -> dict[str, object]:
    data = {
        "number": 27,
        "title": "some PR",
        "headRefOid": NEXT_HEAD,
        "baseRefName": "staging",
        "isDraft": False,
    }
    data.update(overrides)
    return data


def stub_gh(monkeypatch, reviews=None, files=None, compare=None, pulls=None,
            head="b" * 40, base="staging", draft=False):
    """Replace ``gh`` with a router over the endpoints the watcher calls."""
    calls: list[list[str]] = []

    def fake_gh(args: list[str]) -> str:
        calls.append(args)
        joined = " ".join(args)
        if "search/issues" in joined:
            found = pulls if pulls is not None else []
            return json.dumps(
                {
                    "total_count": len(found),
                    "items": [
                        {
                            "number": p["number"],
                            "title": p["title"],
                            "labels": [],
                            "pull_request": {"url": "..."},
                        }
                        for p in found
                    ],
                }
            )
        if "pulls" in joined and "/reviews" in joined:
            return json.dumps(reviews if reviews is not None else [])
        if "/comments" in joined:
            return "[]"
        if "/files" in joined:
            return "\n".join(files if files is not None else [])
        if "/compare/" in joined:
            if compare is None:
                raise GitHubError("no compare")
            return json.dumps(compare)
        if "--jq" in args:
            return json.dumps({"head": head, "base": base, "draft": draft})
        return "{}"

    monkeypatch.setattr(pr_watch, "gh", fake_gh)
    return calls


def search_queries(calls: list[list[str]]) -> list[str]:
    return [" ".join(c) for c in calls if "search/issues" in " ".join(c)]


# --- the decision path ---------------------------------------------------


def test_no_ledger_is_not_a_candidate_for_the_watcher():
    # The watcher only continues an existing loop. Round 1 is triggered by
    # a human asking for it, not by a poll.
    assert evaluate(pull(), None, (), max_rounds=6) is None


def test_new_head_with_a_relevant_delta_is_a_candidate():
    ledger = make_ledger()

    candidate = evaluate(pull(), ledger, ("gryphon/parser.py",), max_rounds=6)

    assert candidate is not None
    assert candidate.number == 27
    assert candidate.round == 2
    assert candidate.open_findings == 1


def test_new_head_with_an_irrelevant_delta_is_skipped():
    ledger = make_ledger()

    assert evaluate(pull(), ledger, ("README.md",), max_rounds=6) is None


def test_same_head_is_skipped():
    ledger = make_ledger()

    assert evaluate(
        pull(headRefOid=HEAD), ledger, ("gryphon/parser.py",), max_rounds=6
    ) is None


def test_retargeted_pr_is_not_a_watcher_candidate():
    # The skill treats a retarget as a full re-review with a human in the
    # loop; the watcher does not restart a ledger on its own.
    ledger = make_ledger()

    assert evaluate(
        pull(baseRefName="main"), ledger, ("gryphon/parser.py",), max_rounds=6
    ) is None


def test_round_cap_is_enforced_by_the_watcher():
    ledger = make_ledger(round=6)

    assert evaluate(pull(), ledger, ("gryphon/parser.py",), max_rounds=6) is None


def test_no_cap_means_the_loop_never_stops_on_its_own():
    ledger = make_ledger(round=99)

    assert evaluate(pull(), ledger, ("gryphon/parser.py",), max_rounds=None) is not None


def test_a_pr_with_no_open_findings_is_skipped():
    ledger = make_ledger(
        findings=(Finding(id=1, status="resolved", sev="high", title="x", files=("a.py",)),)
    )

    assert evaluate(pull(), ledger, ("a.py",), max_rounds=6) is None


def test_empty_delta_list_is_treated_as_relevant():
    ledger = make_ledger()

    assert evaluate(pull(), ledger, (), max_rounds=6) is not None


# --- reading the thread --------------------------------------------------


def test_review_bodies_merge_both_surfaces_newest_first(monkeypatch):
    def fake_gh(args: list[str]) -> str:
        if "/reviews" in " ".join(args):
            return json.dumps(
                [
                    {"body": "older review", "submitted_at": "2026-01-01T00:00:00Z"},
                    {"body": "newest review", "submitted_at": "2026-03-01T00:00:00Z"},
                ]
            )
        return json.dumps(
            [{"body": "issue comment", "created_at": "2026-02-01T00:00:00Z"}]
        )

    monkeypatch.setattr(pr_watch, "gh", fake_gh)

    bodies = fetch_review_bodies(REPO, 27)

    assert bodies[0] == "newest review"
    assert "issue comment" in bodies


def test_watcher_finds_a_ledger_published_as_an_issue_comment(monkeypatch):
    body = "rodada 1\n\n" + render(make_ledger())

    def fake_gh(args: list[str]) -> str:
        if "/reviews" in " ".join(args):
            return "[]"
        return json.dumps([{"body": body, "created_at": "2026-01-01T00:00:00Z"}])

    monkeypatch.setattr(pr_watch, "gh", fake_gh)

    ledger = pr_watch.newest_ledger(REPO, 27)

    assert ledger is not None
    assert ledger.round == 1


# --- delta narrowing -----------------------------------------------------


def test_delta_narrows_to_files_changed_since_the_reviewed_head(monkeypatch):
    stub_gh(
        monkeypatch,
        reviews=[{"body": "x\n" + render(make_ledger()), "submitted_at": "2026-01-01T00:00:00Z"}],
        files=["gryphon/parser.py", "README.md"],
        compare=["gryphon/parser.py"],
    )

    delta = pr_watch.fetch_delta_files(REPO, 27, since=HEAD)

    assert delta == ("gryphon/parser.py",)


def test_delta_falls_back_to_the_full_list_when_compare_is_unavailable(monkeypatch):
    stub_gh(monkeypatch, files=["a.py", "b.py"], compare=None)

    assert pr_watch.fetch_delta_files(REPO, 27, since=HEAD) == ("a.py", "b.py")


def test_delta_with_no_prior_head_returns_every_file(monkeypatch):
    stub_gh(monkeypatch, files=["a.py", "b.py"])

    assert pr_watch.fetch_delta_files(REPO, 27, since="") == ("a.py", "b.py")


# --- round rendering ----------------------------------------------------


def test_round_body_carries_the_prose_and_the_ledger():
    ledger = make_ledger()
    body = pr_watch.render_round_body(
        ledger, ledger.findings, "## primeira passada\n\nVeredito."
    )

    assert body.startswith("## primeira passada")
    assert "1. boom — open" in body
    assert extract(body) == ledger


def test_round_body_orders_findings_by_id():
    ledger = make_ledger(
        findings=(
            Finding(id=7, status="open", sev="low", title="later"),
            Finding(id=2, status="resolved", sev="high", title="earlier"),
        )
    )

    body = pr_watch.render_round_body(ledger, ledger.findings, "prosa")

    assert body.index("2. earlier") < body.index("7. later")


# --- the unattended flag policy -----------------------------------------


def test_a_carried_high_blocker_escalates():
    previous = make_ledger()
    verdicts = (
        Finding(id=1, status="open", sev="high", title="old blocker", files=()),
    )

    assert pr_watch.escalate(previous, verdicts) == "request-changes"


def test_a_brand_new_high_finding_stays_a_comment():
    # The distinction the code has to make: a high finding the agent just
    # invented is its first disagreement with new code, an opinion. Only a
    # finding an earlier round already raised and left unfixed is evidence.
    previous = make_ledger(
        findings=(Finding(id=1, status="open", sev="low", title="nit", files=()),)
    )
    verdicts = (
        Finding(id=1, status="open", sev="low", title="nit", files=()),
        Finding(id=9, status="open", sev="high", title="brand new", files=()),
    )

    assert pr_watch.escalate(previous, verdicts) == "comment"


def test_carried_is_derived_not_asserted():
    # Same verdict, different history. Nothing in the Finding carries
    # "carried"; it comes from the previous ledger's open ids.
    verdicts = (Finding(id=3, status="open", sev="high", title="x", files=()),)

    assert pr_watch.escalate(None, verdicts) == "comment"
    assert pr_watch.escalate(
        make_ledger(findings=(Finding(id=3, status="open", sev="high", title="x", files=()),)),
        verdicts,
    ) == "request-changes"


def test_a_carried_finding_the_agent_resolved_does_not_escalate():
    previous = make_ledger()
    verdicts = (
        Finding(id=1, status="resolved", sev="high", title="fixed", files=()),
    )

    assert pr_watch.escalate(previous, verdicts) == "comment"


def test_a_carried_low_finding_does_not_escalate():
    previous = make_ledger(
        findings=(Finding(id=1, status="open", sev="low", title="nit", files=()),)
    )

    assert pr_watch.escalate(
        previous, (Finding(id=1, status="open", sev="low", title="nit", files=()),)
    ) == "comment"


def test_a_resolved_previous_finding_is_not_a_carried_blocker():
    # Only ids that were *open* last round count as carried. A finding the
    # previous round closed and the agent reopens is new disagreement, not
    # a disconfirmation.
    previous = make_ledger(
        findings=(Finding(id=1, status="resolved", sev="high", title="x", files=()),)
    )

    assert pr_watch.escalate(
        previous, (Finding(id=1, status="open", sev="high", title="x", files=()),)
    ) == "comment"


def test_no_previous_ledger_never_escalates():
    assert pr_watch.escalate(
        None, (Finding(id=1, status="open", sev="high", title="x", files=()),)
    ) == "comment"


def test_escalation_never_returns_approve():
    previous = make_ledger()

    for verdicts in (
        (Finding(id=1, status="open", sev="high", title="x", files=()),),
        (Finding(id=1, status="resolved", sev="high", title="x", files=()),),
        (),
    ):
        assert pr_watch.escalate(previous, verdicts) in ("comment", "request-changes")


# --- gh failure modes ---------------------------------------------------


def test_a_gh_failure_surfaces_as_github_error(monkeypatch):
    def boom(args: list[str]) -> str:
        raise GitHubError("not authenticated")

    monkeypatch.setattr(pr_watch, "gh", boom)

    with pytest.raises(GitHubError):
        survey(REPO)


def test_malformed_pr_list_is_a_github_error(monkeypatch):
    monkeypatch.setattr(pr_watch, "gh", lambda args: "not json")

    with pytest.raises(GitHubError):
        survey(REPO)


def test_draft_pulls_are_excluded(monkeypatch):
    def fake_gh(args: list[str]) -> str:
        joined = " ".join(args)
        if "search/issues" in joined:
            return json.dumps(
                {
                    "items": [
                        {"number": 28, "title": "t", "labels": [],
                         "pull_request": {"url": "..."}},
                    ]
                }
            )
        if "--jq" in args:
            return json.dumps({"head": "a" * 40, "base": "staging", "draft": True})
        return "[]"

    monkeypatch.setattr(pr_watch, "gh", fake_gh)
    monkeypatch.setattr(pr_watch, "resolve_reviewer", lambda: "jjgouveia")

    assert survey(REPO) == []


# --- the derived scope ---------------------------------------------------


def test_the_scan_asks_which_prs_the_reviewer_reviewed(monkeypatch):
    calls = stub_gh(monkeypatch)
    monkeypatch.setattr(pr_watch, "resolve_reviewer", lambda: "jjgouveia")

    survey(REPO, max_rounds=6)

    queries = search_queries(calls)
    assert queries, "the scope must come from the reviewer's reviews"
    assert "reviewed-by:jjgouveia" in queries[0]


def test_a_candidate_says_which_repo_it_came_from(monkeypatch):
    stub_gh(
        monkeypatch,
        pulls=[pull()],
        reviews=[
            {"body": render(make_ledger()), "submitted_at": "2026-01-01T00:00:00Z"}
        ],
        files=["gryphon/parser.py"],
        compare=["gryphon/parser.py"],
        head=NEXT_HEAD,
    )
    monkeypatch.setattr(pr_watch, "resolve_reviewer", lambda: "jjgouveia")

    found = survey(REPO, max_rounds=6)

    assert found[0].repo == REPO


def test_the_reviewers_own_account_is_the_default_scope(monkeypatch):
    calls = stub_gh(monkeypatch)
    monkeypatch.setattr(pr_watch, "resolve_reviewer", lambda: "someone-else")

    survey(REPO, max_rounds=6)

    assert "reviewed-by:someone-else" in search_queries(calls)[0]


def test_an_explicit_reviewer_overrides_the_default(monkeypatch):
    calls = stub_gh(monkeypatch)

    survey(REPO, reviewer="bot", max_rounds=6)

    assert "reviewed-by:bot" in search_queries(calls)[0]


def test_a_label_narrows_the_scope_but_is_not_required(monkeypatch):
    calls = stub_gh(monkeypatch)
    monkeypatch.setattr(pr_watch, "resolve_reviewer", lambda: "jjgouveia")

    survey(REPO, label="on-hold", max_rounds=6)

    # The filter runs on the search payload, not in the query.
    assert "label:" not in search_queries(calls)[0]


def test_a_label_excludes_a_pr_that_lacks_it(monkeypatch):
    def fake_gh(args: list[str]) -> str:
        joined = " ".join(args)
        if "search/issues" in joined:
            return json.dumps(
                {
                    "items": [
                        {
                            "number": 27,
                            "title": "t",
                            "labels": [],
                            "pull_request": {"url": "..."},
                        }
                    ]
                }
            )
        return "{}"

    monkeypatch.setattr(pr_watch, "gh", fake_gh)
    monkeypatch.setattr(pr_watch, "resolve_reviewer", lambda: "jjgouveia")

    assert survey(REPO, label="on-hold", max_rounds=6) == []


def test_a_pr_with_no_open_findings_is_not_scanned_forever(monkeypatch):
    # Everything settled means the author owes nothing; without this the
    # scan would re-review PRs the author already fixed.
    stub_gh(
        monkeypatch,
        pulls=[pull()],
        reviews=[
            {
                "body": render(
                    make_ledger(
                        findings=(
                            Finding(id=1, status="resolved", sev="high", title="x", files=()),
                        )
                    )
                ),
                "submitted_at": "2026-01-01T00:00:00Z",
            }
        ],
        files=["a.py"],
        compare=["a.py"],
    )
    monkeypatch.setattr(pr_watch, "resolve_reviewer", lambda: "jjgouveia")

    assert survey(REPO, max_rounds=6) == []


def test_a_pr_with_an_open_finding_and_a_moved_head_is_a_candidate(monkeypatch):
    stub_gh(
        monkeypatch,
        pulls=[pull()],
        reviews=[
            {"body": render(make_ledger()), "submitted_at": "2026-01-01T00:00:00Z"}
        ],
        files=["gryphon/parser.py"],
        compare=["gryphon/parser.py"],
        head=NEXT_HEAD,
    )
    monkeypatch.setattr(pr_watch, "resolve_reviewer", lambda: "jjgouveia")

    found = survey(REPO, max_rounds=6)

    assert [c.number for c in found] == [27]
    assert found[0].open_findings == 1


def test_a_pr_the_author_already_fixed_stays_out_of_the_scan(monkeypatch):
    # The ledger records the head that was reviewed, so a push that lands
    # before the round runs is still a candidate; but once the round runs
    # and clears the findings, the PR stops being scanned.
    stub_gh(
        monkeypatch,
        pulls=[pull()],
        reviews=[
            {"body": render(make_ledger()), "submitted_at": "2026-01-01T00:00:00Z"}
        ],
        files=["gryphon/parser.py"],
        compare=["gryphon/parser.py"],
        head=HEAD,
    )
    monkeypatch.setattr(pr_watch, "resolve_reviewer", lambda: "jjgouveia")

    assert survey(REPO, max_rounds=6) == []


# --- polling several repositories ----------------------------------------


def test_survey_many_merges_the_repos(monkeypatch):
    def fake_survey(repo, **kwargs):
        return [Candidate(
            number=1 if repo == "a/b" else 2,
            title="t", head="h", base="main", round=2,
            open_findings=1, reason="", repo=repo,
        )]

    monkeypatch.setattr(pr_watch, "survey", fake_survey)

    found = pr_watch.survey_many(["a/b", "c/d"])

    assert [(c.repo, c.number) for c in found] == [("a/b", 1), ("c/d", 2)]


def test_survey_many_survives_a_repo_it_cannot_read(monkeypatch):
    # A repo the account cannot read must not silently disable the watcher
    # everywhere else, but it must be visible rather than merely quiet.
    seen: list[str] = []

    def fake_survey(repo, **kwargs):
        seen.append(repo)
        if repo == "broken/repo":
            raise pr_watch.GitHubError("could not resolve")
        return [Candidate(
            number=1, title="t", head="h", base="main", round=2,
            open_findings=1, reason="", repo=repo,
        )]

    monkeypatch.setattr(pr_watch, "survey", fake_survey)

    found = pr_watch.survey_many(["broken/repo", "good/repo"])

    assert seen == ["broken/repo", "good/repo"]
    assert [c.repo for c in found] == ["good/repo"]


def test_survey_many_resolves_the_reviewer_once(monkeypatch):
    calls = 0

    def fake_resolve():
        nonlocal calls
        calls += 1
        return "me"

    monkeypatch.setattr(pr_watch, "resolve_reviewer", fake_resolve)
    monkeypatch.setattr(pr_watch, "survey", lambda repo, **kwargs: [])

    pr_watch.survey_many(["a/b", "c/d", "e/f"])

    assert calls == 1


def test_the_cli_accepts_a_comma_separated_repo_list():
    result = run_cli("--repo", "jjgouveia/gryphon-standalone,Ativos-Tecnologia/cvld", "--json")

    assert result.returncode in (0, 1), result.stderr
    if result.returncode == 1:
        for entry in json.loads(result.stdout):
            assert entry["repo"] in (
                "jjgouveia/gryphon-standalone",
                "Ativos-Tecnologia/cvld",
            )


# --- CLI -----------------------------------------------------------------


def run_cli(*args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, str(ROOT / "scripts" / "pr_watch.py"), *args],
        capture_output=True,
        text=True,
    )


def test_cli_requires_a_repo():
    assert run_cli().returncode == 2


def test_cli_reports_a_bad_repo():
    assert run_cli("--repo", "jjgouveia/definitely-not-a-repo-xyz").returncode == 2


@pytest.mark.skipif(
    subprocess.run(["gh", "api", "user"], capture_output=True).returncode != 0,
    reason="gh not authenticated",
)
def test_the_live_scan_finds_prs_this_reviewer_reviewed():
    # The scope is derived, so the live queue is whatever the reviewer has
    # open PRs with unresolved findings on. Asserting a specific count would
    # break the moment the author pushes; what matters is that the real gh
    # plumbing resolves the reviewer and reads the thread.
    result = run_cli("--repo", REPO, "--json")

    assert result.returncode in (0, 1), result.stderr
    if result.returncode == 1:
        found = json.loads(result.stdout)
        assert found, "exit 1 with an empty list would be a lie"
        for entry in found:
            assert entry["round"] >= 2
            assert entry["open_findings"] >= 1
            assert len(entry["head"]) == 40


@pytest.mark.skipif(
    subprocess.run(["gh", "api", "user"], capture_output=True).returncode != 0,
    reason="gh not authenticated",
)
def test_the_live_scope_needs_no_label():
    # A review published with --request-changes is the trigger, and nothing
    # has to be applied by hand for the PR to enter the scan.
    result = run_cli("--repo", REPO, "--json")

    assert "gryphon:watch" not in result.stdout
    assert "--label" not in result.stderr
