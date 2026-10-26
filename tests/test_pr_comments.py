"""Tests for reading and reacting to a PR's developer comments.

``gh`` is stubbed, so the merge over the three thread surfaces, the
last-round cut and the reaction endpoints are exercised without a network
call. The last group checks the argument contract, which must fail before
any request is made.
"""

from __future__ import annotations

import json
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

import pytest

ROOT = Path(__file__).parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import pr_comments  # noqa: E402
from pr_comments import (  # noqa: E402
    Comment,
    fetch_comments,
    react,
    reply,
    select,
)
from pr_comments import render as render_comments  # noqa: E402
from pr_ledger import Finding, Ledger, render  # noqa: E402

REPO = "Ativos-Labs/api-celer"
PR = 490
PR_URL = f"https://github.com/{REPO}/pull/{PR}"
AUTHOR = "MarinaldoSilva"
REVIEWER = "jjgouveia"


def raw(
    ident: int,
    body: str,
    created: str,
    *,
    login: str = AUTHOR,
    kind: str = "User",
    **extra: object,
) -> dict[str, object]:
    return {
        "id": ident,
        "body": body,
        "user": {"login": login, "type": kind},
        "created_at": created,
        "html_url": f"{PR_URL}#issuecomment-{ident}",
        **extra,
    }


def review(
    ident: int,
    body: str,
    submitted: str,
    *,
    login: str = REVIEWER,
    kind: str = "User",
) -> dict[str, object]:
    return {
        "id": ident,
        "body": body,
        "user": {"login": login, "type": kind},
        "submitted_at": submitted,
    }


def ledger_body(findings: tuple[Finding, ...] = ()) -> str:
    return "round\n\n" + render(
        Ledger(
            head="a" * 40,
            base="staging",
            round=1,
            flag="request-changes",
            findings=findings,
        )
    )


def stub_thread(
    monkeypatch,
    *,
    issue: list[dict[str, object]] | None = None,
    inline: list[dict[str, object]] | None = None,
    reviews: list[dict[str, object]] | None = None,
) -> list[list[str]]:
    """Route ``gh`` by surface and record every call."""
    calls: list[list[str]] = []

    def fake_gh(args: list[str]) -> str:
        calls.append(args)
        joined = " ".join(args)
        if f"pulls/{PR}/comments" in joined:
            return json.dumps(inline if inline is not None else [])
        if f"pulls/{PR}/reviews" in joined:
            return json.dumps(reviews if reviews is not None else [])
        if f"issues/{PR}/comments" in joined:
            return json.dumps(issue if issue is not None else [])
        if joined.endswith(f"pulls/{PR}"):
            return json.dumps(
                {"user": {"login": AUTHOR}, "html_url": PR_URL}
            )
        raise AssertionError(f"unexpected gh call: {joined}")

    monkeypatch.setattr(pr_comments, "gh", fake_gh)
    return calls


def sample_comment(**overrides: object) -> Comment:
    values: dict[str, object] = {
        "id": 11,
        "kind": "issue",
        "author": AUTHOR,
        "body": "Corrigi os bloqueios.",
        "created_at": "2026-10-23T18:00:00Z",
        "url": PR_URL,
        "is_author": True,
    }
    values.update(overrides)
    return Comment(**values)  # type: ignore[arg-type]


# --- reading the thread --------------------------------------------------


def test_the_three_surfaces_merge_oldest_first(monkeypatch):
    stub_thread(
        monkeypatch,
        issue=[raw(1, "na conversa", "2026-10-23T18:02:00Z")],
        inline=[
            raw(
                2,
                "no diff",
                "2026-10-23T18:03:00Z",
                path="app/x.py",
                line=42,
            )
        ],
        reviews=[review(3, "revisão formal", "2026-10-23T18:01:00Z")],
    )

    comments = fetch_comments(REPO, PR)

    assert [c.kind for c in comments] == ["review", "issue", "inline"]
    assert comments[-1].path == "app/x.py"
    assert comments[-1].line == 42


def test_the_pr_author_is_flagged_and_other_people_are_not(monkeypatch):
    stub_thread(
        monkeypatch,
        issue=[
            raw(1, "sou o autor", "2026-10-23T18:00:00Z", login=AUTHOR),
            raw(2, "sou outro dev", "2026-10-23T18:01:00Z", login="RobsonFe"),
        ],
    )

    comments = fetch_comments(REPO, PR)

    assert [(c.author, c.is_author) for c in comments] == [
        (AUTHOR, True),
        ("RobsonFe", False),
    ]


def test_bots_and_empty_bodies_are_not_comments_to_answer(monkeypatch):
    stub_thread(
        monkeypatch,
        issue=[
            raw(1, "   ", "2026-10-23T18:00:00Z"),
            raw(2, "comentário de bot", "2026-10-23T18:01:00Z", login="ci[bot]", kind="Bot"),
            raw(3, "comentário humano", "2026-10-23T18:02:00Z"),
        ],
    )

    comments = fetch_comments(REPO, PR)

    assert [c.id for c in comments] == [3]


def test_a_gryphon_review_is_marked_as_a_round_not_dropped(monkeypatch):
    stub_thread(
        monkeypatch,
        reviews=[review(9, ledger_body(), "2026-10-23T18:00:00Z")],
    )

    comments = fetch_comments(REPO, PR)

    assert comments[0].has_ledger is True


def test_last_round_cuts_at_the_newest_ledger(monkeypatch):
    stub_thread(
        monkeypatch,
        issue=[raw(4, "ainda não era a resposta", "2026-10-23T18:05:00Z")],
        reviews=[
            review(9, ledger_body(), "2026-10-23T18:00:00Z"),
            review(10, ledger_body(), "2026-10-23T18:10:00Z"),
        ],
    )
    comments = fetch_comments(REPO, PR)

    selected = select(comments, since="last-round")

    assert selected == []


def test_a_bot_review_carrying_a_ledger_still_sets_the_cut(monkeypatch):
    # In CI the review is posted as github-actions[bot]. It must not be
    # shown, but it is the round marker: dropping it would read the whole
    # thread again on every pass.
    stub_thread(
        monkeypatch,
        issue=[raw(4, "resposta depois da rodada", "2026-10-23T18:30:00Z")],
        reviews=[
            review(
                9,
                ledger_body(),
                "2026-10-23T18:00:00Z",
                login="github-actions[bot]",
                kind="Bot",
            )
        ],
    )
    comments = fetch_comments(REPO, PR)

    selected = select(comments, since="last-round")

    assert [c.id for c in selected] == [4]
    assert 9 in {c.id for c in select(comments, include_ledger=True)}


def test_last_round_shows_only_what_came_after_it(monkeypatch):
    stub_thread(
        monkeypatch,
        issue=[
            raw(4, "antes da rodada", "2026-10-23T17:00:00Z"),
            raw(5, "depois da rodada", "2026-10-23T18:30:00Z"),
        ],
        reviews=[review(9, ledger_body(), "2026-10-23T18:00:00Z")],
    )
    comments = fetch_comments(REPO, PR)

    selected = select(comments, since="last-round")

    assert [c.id for c in selected] == [5]


def test_a_thread_with_no_round_shows_everything(monkeypatch):
    stub_thread(
        monkeypatch,
        issue=[raw(1, "primeira resposta", "2026-10-23T18:00:00Z")],
    )
    comments = fetch_comments(REPO, PR)

    assert select(comments, since="last-round") == comments


def test_author_only_hides_other_people(monkeypatch):
    stub_thread(
        monkeypatch,
        issue=[
            raw(1, "autor", "2026-10-23T18:00:00Z"),
            raw(2, "colega", "2026-10-23T18:01:00Z", login="RobsonFe"),
        ],
    )
    comments = fetch_comments(REPO, PR)

    assert [c.id for c in select(comments, author_only=True)] == [1]


def test_the_ledger_body_is_hidden_unless_asked_for(monkeypatch):
    stub_thread(
        monkeypatch,
        reviews=[review(9, ledger_body(), "2026-10-23T18:00:00Z")],
    )
    comments = fetch_comments(REPO, PR)

    assert select(comments) == []
    assert [c.id for c in select(comments, include_ledger=True)] == [9]


# --- showing the replies -------------------------------------------------


def test_render_shows_author_body_and_link():
    text = render_comments([sample_comment()], pr=PR)

    assert "## Comentários no PR #490" in text
    assert "**@MarinaldoSilva** (autor)" in text
    assert "> Corrigi os bloqueios." in text
    assert "[ver no PR]" in text


def test_render_marks_the_inline_location():
    text = render_comments(
        [sample_comment(kind="inline", path="app/x.py", line=42, is_author=False)],
        pr=PR,
    )

    assert "`app/x.py:42`" in text


def test_render_is_honest_when_there_is_nothing_new():
    assert render_comments([], pr=PR) == "Nenhum comentário novo no PR."


def test_render_clips_a_pasted_log():
    long_body = "x" * (pr_comments.BODY_LIMIT + 50)

    text = render_comments([sample_comment(body=long_body)], pr=PR)

    assert "truncado" in text
    assert len(text) < pr_comments.BODY_LIMIT + 300


# --- reacting ------------------------------------------------------------


def test_react_uses_the_endpoint_for_each_surface(monkeypatch):
    calls: list[list[str]] = []

    def fake_gh(args: list[str]) -> str:
        calls.append(args)
        return "{}"

    monkeypatch.setattr(pr_comments, "gh", fake_gh)
    comments = [
        sample_comment(kind="issue"),
        sample_comment(id=12, kind="inline"),
        sample_comment(id=13, kind="review"),
    ]

    actions = react(REPO, PR, comments, "+1")

    paths = [call[1] for call in calls]
    assert paths == [
        f"repos/{REPO}/issues/comments/11/reactions",
        f"repos/{REPO}/pulls/comments/12/reactions",
        f"repos/{REPO}/pulls/{PR}/reviews/13/reactions",
    ]
    assert all(call[call.index("content=+1")] == "content=+1" for call in calls)
    assert len(actions) == 3


def test_a_dry_run_reaction_posts_nothing(monkeypatch):
    def boom(args: list[str]) -> str:
        raise AssertionError("a dry run must not call gh")

    monkeypatch.setattr(pr_comments, "gh", boom)

    actions = react(REPO, PR, [sample_comment()], "eyes", dry_run=True)

    assert actions[0]["dry_run"] is True
    assert actions[0]["reaction"] == "eyes"


def test_an_unknown_reaction_is_rejected():
    with pytest.raises(ValueError):
        react(REPO, PR, [sample_comment()], "clap")


# --- answering -----------------------------------------------------------


def test_an_inline_reply_stays_in_the_thread(monkeypatch):
    calls: list[list[str]] = []

    def fake_gh(args: list[str]) -> str:
        calls.append(args)
        return "{}"

    monkeypatch.setattr(pr_comments, "gh", fake_gh)

    reply(REPO, PR, sample_comment(id=12, kind="inline"), "Confirmado em app/x.py:42.")

    assert calls[0][1] == f"repos/{REPO}/pulls/{PR}/comments/12/replies"
    assert "body=Confirmado em app/x.py:42." in calls[0]


def test_a_conversation_reply_quotes_and_mentions(monkeypatch):
    calls: list[list[str]] = []

    def fake_gh(args: list[str]) -> str:
        calls.append(args)
        return "{}"

    monkeypatch.setattr(pr_comments, "gh", fake_gh)

    reply(REPO, PR, sample_comment(body="Corrigi os dois bloqueios."), "O #4 segue aberto.")

    body = calls[0][-1]
    assert calls[0][1] == f"repos/{REPO}/issues/{PR}/comments"
    assert body.startswith("body=")
    assert "> Corrigi os dois bloqueios." in body
    assert f"@{AUTHOR} O #4 segue aberto." in body


def test_an_empty_reply_is_rejected(monkeypatch):
    monkeypatch.setattr(pr_comments, "gh", lambda args: "{}")

    with pytest.raises(ValueError):
        reply(REPO, PR, sample_comment(), "   ")


# --- the small helpers ---------------------------------------------------


def test_as_dict_carries_the_json_contract():
    data = sample_comment().as_dict()

    assert data["kind"] == "issue"
    assert data["is_author"] is True
    assert data["has_ledger"] is False
    assert data["in_reply_to_id"] is None


def test_parse_ts_accepts_the_github_zulu_stamp():
    parsed = pr_comments._parse_ts("2026-10-23T18:00:00Z")

    assert parsed.tzinfo is not None
    with pytest.raises(ValueError):
        pr_comments._parse_ts("ontem")


def test_a_malformed_timestamp_does_not_hide_the_comment():
    # Ordering is a convenience; the body is the evidence. A stamp the
    # parser cannot read sorts first instead of taking the comment with it.
    assert pr_comments._timestamp("nonsense") == datetime.min.replace(
        tzinfo=timezone.utc
    )
    assert pr_comments._timestamp("2026-01-01T00:00:00Z") > pr_comments._timestamp(
        "nonsense"
    )


def test_bot_detection_covers_the_type_and_the_login_suffix():
    assert pr_comments._is_bot({"login": "ci[bot]", "type": "User"}) is True
    assert pr_comments._is_bot({"login": "someone", "type": "Bot"}) is True
    assert pr_comments._is_bot({"login": "someone", "type": "User"}) is False


def test_review_url_points_at_the_review_anchor():
    assert pr_comments._review_url(PR_URL, 9) == f"{PR_URL}#pullrequestreview-9"
    assert pr_comments._review_url("", 9) == ""


def test_clip_leaves_a_short_body_alone_and_marks_the_cut():
    assert pr_comments._clip("curto", 100) == "curto"

    clipped = pr_comments._clip("x" * 50, 10)

    assert clipped.startswith("x" * 10)
    assert "truncado" in clipped


# --- the argument contract ----------------------------------------------


def run_cli(*args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, str(ROOT / "scripts" / "pr_comments.py"), *args],
        capture_output=True,
        text=True,
    )


def test_cli_requires_the_repo_and_the_pr():
    assert run_cli().returncode == 2


def test_cli_rejects_an_unreadable_since_before_calling_gh():
    # No network: the request is unusable, so it fails at the argument.
    result = run_cli("--repo", REPO, "--pr", "490", "--since", "ontem")

    assert result.returncode == 3
    assert "ISO timestamp" in result.stderr


def test_cli_rejects_a_reply_without_a_target():
    assert run_cli("--repo", REPO, "--pr", "490", "--reply-text", "oi").returncode == 3


def test_cli_rejects_a_target_without_a_reply():
    assert run_cli("--repo", REPO, "--pr", "490", "--reply-id", "11").returncode == 3


def test_stdout_is_switched_to_utf8(monkeypatch):
    # A reply with an arrow or an emoji must not kill the print on a
    # Windows console whose code page cannot represent it.
    seen: dict[str, object] = {}

    class FakeStdout:
        def reconfigure(self, **kwargs: object) -> None:
            seen.update(kwargs)

    monkeypatch.setattr(pr_comments.sys, "stdout", FakeStdout())

    pr_comments._configure_stdout()

    assert seen == {"encoding": "utf-8", "errors": "replace"}
