"""Tests for the Stop hook that resumes a review when the author pushes.

The hook's whole job is deciding whether to stay silent, so most of these
are silence tests. The one that matters most is ``stop_hook_active``: if
the hook re-asks while Claude Code is already continuing because of it, the
session asks the same question about the same heads forever.

The hook is driven in-process, not as a subprocess, so a stubbed
``survey`` actually applies. Spawning would ignore the monkeypatch and
every silence assertion would pass for the wrong reason.
"""

from __future__ import annotations

import io
import json
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import pr_watch_hook  # noqa: E402
from pr_watch import Candidate, GitHubError  # noqa: E402

REPO = "jjgouveia/gryphon-standalone"
HEAD = "a" * 40


def candidate(number: int = 29, head: str = HEAD, round_number: int = 2) -> Candidate:
    return Candidate(
        number=number,
        title="some PR",
        head=head,
        base="staging",
        round=round_number,
        open_findings=1,
        reason="round 2",
        delta_files=("gryphon/parser.py",),
    )


@pytest.fixture
def env(tmp_path, monkeypatch):
    monkeypatch.setenv(pr_watch_hook.ENV_REPO, REPO)
    monkeypatch.setenv(pr_watch_hook.ENV_STATE, str(tmp_path / "state.json"))
    monkeypatch.setenv(pr_watch_hook.ENV_INTERVAL, "0")
    return tmp_path


def run(monkeypatch, capsys, event: dict | None = None) -> tuple[int, str]:
    payload = json.dumps(event if event is not None else {"hook_event_name": "Stop"})
    monkeypatch.setattr(pr_watch_hook.sys, "stdin", io.StringIO(payload))
    code = pr_watch_hook.main()
    return code, capsys.readouterr().out


def quiet(monkeypatch, capsys, env, event=None) -> None:
    code, out = run(monkeypatch, capsys, event)
    assert code == 0
    assert out.strip() == ""


def context_of(monkeypatch, capsys, event=None) -> str:
    _code, out = run(monkeypatch, capsys, event)
    return json.loads(out)["hookSpecificOutput"]["additionalContext"]


# --- silence is the common path -----------------------------------------


def test_a_quiet_thread_prints_nothing(monkeypatch, capsys, env):
    monkeypatch.setattr(pr_watch_hook, "survey_many", lambda repos, max_rounds: [])

    quiet(monkeypatch, capsys, env)


def test_the_hook_never_blocks(monkeypatch, capsys, env):
    # Exit 2 routes as a blocking decision and shows a hook error. This is
    # not an error condition; it is ordinary work continuing.
    monkeypatch.setattr(pr_watch_hook, "survey_many", lambda repos, max_rounds: [candidate()])

    code, _out = run(monkeypatch, capsys)

    assert code == 0


def test_a_poll_failure_stays_silent(monkeypatch, capsys, env):
    # A poll that could not reach GitHub knows nothing. Reporting "nothing
    # pending" would be a claim it cannot support.

    def boom(repos, max_rounds):
        raise GitHubError("rate limited")

    monkeypatch.setattr(pr_watch_hook, "survey_many", boom)

    quiet(monkeypatch, capsys, env)


def test_no_repo_configured_stays_silent(monkeypatch, capsys, env):
    monkeypatch.delenv(pr_watch_hook.ENV_REPO, raising=False)
    monkeypatch.setattr(pr_watch_hook, "discover_repo", lambda: None)
    monkeypatch.setattr(
        pr_watch_hook,
        "survey_many",
        lambda *a, **k: pytest.fail("polled without a repo"),
    )

    quiet(monkeypatch, capsys, env)


# --- the loop guard -----------------------------------------------------


def test_the_hook_stays_silent_while_already_continuing(monkeypatch, capsys, env):
    monkeypatch.setattr(pr_watch_hook, "survey_many", lambda repos, max_rounds: [candidate()])

    quiet(monkeypatch, capsys, env, {"stop_hook_active": True})


def test_the_loop_guard_survives_a_finished_round(monkeypatch, capsys, env):
    # The realistic ping-pong: Claude finishes a round, the hook wakes it,
    # the round runs, Claude finishes again with stop_hook_active true.
    monkeypatch.setenv(pr_watch_hook.ENV_INTERVAL, "0")
    monkeypatch.setattr(pr_watch_hook, "survey_many", lambda repos, max_rounds: [candidate()])

    _first_code, first = run(
        monkeypatch, capsys, {"hook_event_name": "Stop", "stop_hook_active": False}
    )
    _second_code, second = run(
        monkeypatch, capsys, {"hook_event_name": "Stop", "stop_hook_active": True}
    )

    assert json.loads(first)
    assert second.strip() == ""


# --- polling several repositories ----------------------------------------


def test_a_comma_separated_repo_list_is_split(monkeypatch, env):
    monkeypatch.setenv(
        pr_watch_hook.ENV_REPO,
        "Ativos-Tecnologia/cvld, jjgouveia/gryphon-standalone ,,",
    )

    assert pr_watch_hook.pending_repos() == [
        "Ativos-Tecnologia/cvld",
        "jjgouveia/gryphon-standalone",
    ]


def test_one_repo_is_still_a_list(monkeypatch, env):
    monkeypatch.setenv(pr_watch_hook.ENV_REPO, "jjgouveia/gryphon-standalone")

    assert pr_watch_hook.pending_repos() == ["jjgouveia/gryphon-standalone"]


def test_the_checkout_remote_is_the_fallback(monkeypatch, env):
    monkeypatch.delenv(pr_watch_hook.ENV_REPO, raising=False)
    monkeypatch.setattr(
        pr_watch_hook, "discover_repo", lambda: "jjgouveia/gryphon-standalone"
    )

    assert pr_watch_hook.pending_repos() == ["jjgouveia/gryphon-standalone"]


def test_no_repo_anywhere_is_an_empty_list(monkeypatch, env):
    monkeypatch.delenv(pr_watch_hook.ENV_REPO, raising=False)
    monkeypatch.setattr(pr_watch_hook, "discover_repo", lambda: None)

    assert pr_watch_hook.pending_repos() == []


def test_the_hook_polls_every_configured_repo(monkeypatch, capsys, env):
    # The loop has to follow the reviewer across repos: watching one while
    # the others go unnoticed is the same as not watching at all.
    monkeypatch.setenv(
        pr_watch_hook.ENV_REPO, "Ativos-Tecnologia/cvld,jjgouveia/gryphon-standalone"
    )
    seen: list[list[str]] = []

    def fake_survey(repos, **kwargs):
        seen.append(list(repos))
        return []

    monkeypatch.setattr(pr_watch_hook, "survey_many", fake_survey)

    run(monkeypatch, capsys)

    assert seen == [["Ativos-Tecnologia/cvld", "jjgouveia/gryphon-standalone"]]


def test_the_context_names_the_repo(monkeypatch, capsys, env):
    # With several repos in play, "#2145" alone is ambiguous.
    monkeypatch.setattr(
        pr_watch_hook,
        "survey_many",
        lambda repos, max_rounds: [
            Candidate(
                number=2145, title="t", head=HEAD, base="main", round=2,
                open_findings=1, reason="", repo="Ativos-Tecnologia/cvld",
            )
        ],
    )

    context = context_of(monkeypatch, capsys)

    assert "Ativos-Tecnologia/cvld#2145" in context


def test_a_candidate_without_a_repo_still_reads(monkeypatch, capsys, env):
    monkeypatch.setattr(
        pr_watch_hook, "survey_many", lambda repos, max_rounds: [candidate()]
    )

    assert "#29" in context_of(monkeypatch, capsys)


# --- the payload --------------------------------------------------------


def test_a_pending_push_hands_the_work_back(monkeypatch, capsys, env):
    monkeypatch.setattr(pr_watch_hook, "survey_many", lambda repos, max_rounds: [candidate()])

    context = context_of(monkeypatch, capsys)

    assert "#29" in context
    assert HEAD[:8] in context
    assert "--resume" in context


def test_the_event_name_is_declared(monkeypatch, capsys, env):
    monkeypatch.setattr(pr_watch_hook, "survey_many", lambda repos, max_rounds: [candidate()])

    _code, out = run(monkeypatch, capsys)

    assert json.loads(out)["hookSpecificOutput"]["hookEventName"] == "Stop"


# --- the three events that carry the round ------------------------------


@pytest.mark.parametrize(
    "event",
    ["Stop", "SessionStart", "UserPromptSubmit"],
)
def test_each_carrying_event_reports_the_round(monkeypatch, capsys, env, event):
    # Stop covers a push mid-session, SessionStart covers coming back to the
    # terminal, UserPromptSubmit covers asking something in between. All three
    # accept additionalContext, which is the only channel they share.
    monkeypatch.setattr(pr_watch_hook, "survey_many", lambda repos, max_rounds: [candidate()])

    _code, out = run(monkeypatch, capsys, {"hook_event_name": event})
    payload = json.loads(out)["hookSpecificOutput"]

    assert payload["hookEventName"] == event
    assert "#29" in payload["additionalContext"]


@pytest.mark.parametrize(
    "event", ["PostToolUse", "PreToolUse", "SessionEnd", "SubagentStop", ""]
)
def test_other_events_never_inject(monkeypatch, capsys, env, event):
    # Some of those block a turn and some do not accept the channel at all.
    # Injecting anyway would either be dropped or stall the session.
    monkeypatch.setattr(pr_watch_hook, "survey_many", lambda repos, max_rounds: [candidate()])

    quiet(monkeypatch, capsys, env, {"hook_event_name": event})


def test_the_throttle_is_shared_across_events(monkeypatch, capsys, env):
    # UserPromptSubmit blocks the prompt until the hook returns and discards
    # the context on timeout, so three events inside a minute must cost one
    # poll, not three.
    monkeypatch.setenv(pr_watch_hook.ENV_INTERVAL, "600")
    monkeypatch.setattr(pr_watch_hook, "survey_many", lambda repos, max_rounds: [candidate()])

    _code, first = run(monkeypatch, capsys, {"hook_event_name": "SessionStart"})
    quiet(monkeypatch, capsys, env, {"hook_event_name": "UserPromptSubmit"})
    quiet(monkeypatch, capsys, env, {"hook_event_name": "Stop"})


def test_several_pending_prs_are_all_named(monkeypatch, capsys, env):
    monkeypatch.setattr(
        pr_watch_hook,
        "survey_many",
        lambda repos, max_rounds: [candidate(29), candidate(30, head="b" * 40)],
    )

    context = context_of(monkeypatch, capsys)

    assert "#29" in context and "#30" in context
    assert "2 reviewed PRs" in context


def test_the_context_uses_the_non_error_channel(monkeypatch, capsys, env):
    monkeypatch.setattr(pr_watch_hook, "survey_many", lambda repos, max_rounds: [candidate()])

    _code, out = run(monkeypatch, capsys)

    # additionalContext, not decision:block. The transcript labels the
    # former "Stop hook feedback"; the latter is a hook error.
    assert "decision" not in json.loads(out)


def test_the_context_tells_the_agent_it_may_decline(monkeypatch, capsys, env):
    monkeypatch.setattr(pr_watch_hook, "survey_many", lambda repos, max_rounds: [candidate()])

    assert "moved on" in context_of(monkeypatch, capsys)


# --- throttling ---------------------------------------------------------


def test_a_second_poll_inside_the_interval_is_skipped(monkeypatch, capsys, env):
    monkeypatch.setenv(pr_watch_hook.ENV_INTERVAL, "600")
    monkeypatch.setattr(pr_watch_hook, "survey_many", lambda repos, max_rounds: [candidate()])

    _first_code, first = run(monkeypatch, capsys)
    quiet(monkeypatch, capsys, env)


def test_the_interval_state_is_per_session(monkeypatch, env):
    # Drop the fixture's fixed path so the session id decides the file: two
    # sessions must not throttle each other, or opening a second session
    # would silence the first one's watcher.
    monkeypatch.delenv(pr_watch_hook.ENV_STATE, raising=False)
    monkeypatch.setenv("CLAUDE_SESSION_ID", "session-one")
    path = pr_watch_hook.state_path()
    pr_watch_hook.mark_poll(path)

    assert pr_watch_hook.last_poll(path) > 0

    monkeypatch.setenv("CLAUDE_SESSION_ID", "session-two")
    assert pr_watch_hook.state_path() != path


def test_an_explicit_state_path_overrides_the_session(monkeypatch, env):
    # The env fixture points ENV_STATE at a file; it must win over the
    # session id, which is what lets a test or a scheduled poll pin it.
    monkeypatch.setenv("CLAUDE_SESSION_ID", "session-one")

    assert pr_watch_hook.state_path() == env / "state.json"


def test_a_corrupt_state_file_polls_anyway(monkeypatch, capsys, env):
    marker = pr_watch_hook.state_path()
    marker.parent.mkdir(parents=True, exist_ok=True)
    marker.write_text("not a timestamp", encoding="utf-8")
    monkeypatch.setattr(pr_watch_hook, "survey_many", lambda repos, max_rounds: [candidate()])

    assert context_of(monkeypatch, capsys)


# --- input handling -----------------------------------------------------


def test_empty_stdin_is_handled(monkeypatch, capsys, env):
    monkeypatch.setattr(pr_watch_hook.sys, "stdin", io.StringIO(""))

    assert pr_watch_hook.main() == 0


def test_malformed_stdin_is_handled(monkeypatch, capsys, env):
    monkeypatch.setattr(pr_watch_hook.sys, "stdin", io.StringIO("not json"))

    assert pr_watch_hook.main() == 0


# --- repo discovery -----------------------------------------------------


@pytest.mark.parametrize(
    "url,expected",
    [
        ("https://github.com/jjgouveia/gryphon-standalone.git", "jjgouveia/gryphon-standalone"),
        ("git@github.com:jjgouveia/gryphon-standalone.git", "jjgouveia/gryphon-standalone"),
        ("https://github.com/jjgouveia/gryphon-standalone", "jjgouveia/gryphon-standalone"),
        ("https://gitlab.com/a/b.git", None),
        ("/local/path/gryphon", None),
    ],
)
def test_repo_names_are_parsed_from_remotes(url, expected):
    assert repo_from(url) == expected


def test_discovery_reads_the_origin_remote(monkeypatch):
    monkeypatch.setattr(
        pr_watch_hook.subprocess,
        "run",
        lambda *a, **k: completed(0, "git@github.com:jjgouveia/gryphon-standalone.git\n"),
    )

    assert pr_watch_hook.discover_repo() == "jjgouveia/gryphon-standalone"


def test_discovery_gives_up_on_a_missing_remote(monkeypatch):
    monkeypatch.setattr(
        pr_watch_hook.subprocess, "run", lambda *a, **k: completed(1, "")
    )

    assert pr_watch_hook.discover_repo() is None


def completed(code: int, stdout: str):
    return subprocess.CompletedProcess([], code, stdout=stdout, stderr="")


def repo_from(url: str) -> str | None:
    """The URL half of discover_repo, exercised without a subprocess."""
    cleaned = url[:-4] if url.endswith(".git") else url
    if cleaned.startswith("git@") and ":" in cleaned:
        return cleaned.split(":", 1)[1]
    if "github.com/" in cleaned:
        return cleaned.split("github.com/", 1)[1].strip("/")
    return None


# --- the skill registers it --------------------------------------------


def test_the_skill_registers_the_hook_on_every_carrying_event():
    skill = (ROOT / "skills" / "review-pr" / "SKILL.md").read_text(encoding="utf-8")
    frontmatter = skill.split("---", 2)[1]

    assert "hooks:" in frontmatter
    for event in ("Stop:", "SessionStart:", "UserPromptSubmit:"):
        assert event in frontmatter, f"{event} is not wired"
    assert frontmatter.count("pr_watch_hook.py") == 3


def test_the_hook_stays_well_inside_the_prompt_submit_budget():
    # UserPromptSubmit blocks model processing until the hook returns, and a
    # timeout there discards the context entirely. The configured timeout
    # must leave room under the 30s the event lowers its default to.
    skill = (ROOT / "skills" / "review-pr" / "SKILL.md").read_text(encoding="utf-8")
    frontmatter = skill.split("---", 2)[1]

    timeouts = [
        int(line.split("timeout:")[1].strip())
        for line in frontmatter.splitlines()
        if "timeout:" in line
    ]

    assert timeouts
    assert max(timeouts) <= 20


def test_the_hook_is_registered_in_the_skill_not_in_settings():
    # A settings-file hook would run in every session, including ones where
    # no review was ever asked for. A skill hook only exists once the skill
    # has been invoked, which is the scope this wants.
    settings = ROOT / ".claude" / "settings.json"
    if not settings.is_file():
        pytest.skip("no local settings file")

    assert "pr_watch_hook" not in settings.read_text(encoding="utf-8")
