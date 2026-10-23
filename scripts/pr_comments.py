#!/usr/bin/env python3
"""Read the developer comments on a PR and react to them.

The ledger says what the review found; the thread says what the author did
about it. A re-review round reads the diff, and the author's replies
("both blockers are fixed", "I merged main for the migration") are exactly
the claims the diff cannot be trusted to confirm on its own. This module is
the reading half: it merges the three GitHub surfaces, keeps the comments a
human wrote, and stops at the newest review round so the same replies are
not shown twice.

Reacting is part of the same pass. A reply is an answer the author is
waiting for, and the round should leave a trace on the comment itself:

* ``eyes`` - the reply was read and is being verified;
* ``+1`` - every claim in it holds at head;
* ``confused`` - a claim does not hold; the reply body says which finding.

A reaction is not a review verdict. It costs no merge gate and it is the
cheapest way to close the loop while the round is still running.

    # what the author said since the last round
    python scripts/pr_comments.py --repo <o>/<r> --pr <n> --since last-round

    # only the PR author's comments, as JSON
    python scripts/pr_comments.py --repo <o>/<r> --pr <n> --author-only --json

    # acknowledge and answer
    python scripts/pr_comments.py --repo <o>/<r> --pr <n> --since last-round \
        --react eyes
    python scripts/pr_comments.py --repo <o>/<r> --pr <n> --since last-round \
        --reply-id 123456 --reply-text "Confirmado em src/x.ts:42."

Exit codes:
    0  comments read (and, if asked, reacted to or answered)
    2  gh could not read or write the thread
    3  the arguments did not make a usable request
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable, Literal

sys.path.insert(0, str(Path(__file__).parent))

from pr_ledger import MARKER
from pr_watch import GitHubError, gh

logger = logging.getLogger("pr_comments")

REACTIONS = ("+1", "-1", "laugh", "hooray", "confused", "heart", "rocket", "eyes")

Kind = Literal["issue", "inline", "review"]

# A comment longer than this is evidence, not prose: the full text is one
# click away in the PR, and a round should not spend its context on a
# pasted log. The link stays, so nothing is hidden from the reader.
BODY_LIMIT = 2000

# How much of the original comment is quoted when a reply has to be posted
# as a new conversation comment: GitHub only threads inline review replies.
QUOTE_LIMIT = 400


@dataclass(frozen=True)
class Comment:
    """One human comment, from whichever surface it arrived on."""

    id: int
    kind: Kind
    author: str
    body: str
    created_at: str
    url: str
    is_author: bool = False
    has_ledger: bool = False
    path: str = ""
    line: int | None = None
    in_reply_to_id: int | None = None

    def as_dict(self) -> dict[str, object]:
        return {
            "id": self.id,
            "kind": self.kind,
            "author": self.author,
            "body": self.body,
            "created_at": self.created_at,
            "url": self.url,
            "is_author": self.is_author,
            "has_ledger": self.has_ledger,
            "path": self.path,
            "line": self.line,
            "in_reply_to_id": self.in_reply_to_id,
        }


def _parse_ts(value: str) -> datetime:
    """Parse a GitHub timestamp. ``Z`` is not accepted before Python 3.11."""
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def _timestamp(value: str) -> datetime:
    try:
        return _parse_ts(value)
    except ValueError:
        # A malformed timestamp must not hide the comment: ordering is a
        # convenience, the body is the evidence.
        return datetime.min.replace(tzinfo=timezone.utc)


def _is_bot(user: dict) -> bool:
    login = str(user.get("login") or "")
    return user.get("type") == "Bot" or login.endswith("[bot]")


def _review_url(pr_url: str, review_id: int) -> str:
    if not pr_url:
        return ""
    return f"{pr_url}#pullrequestreview-{review_id}"


def _collect(
    repo: str,
    path: str,
    kind: Kind,
    *,
    author: str,
    pr_url: str,
) -> list[Comment]:
    """One surface of the thread, with bots and empty bodies dropped."""
    try:
        payload = json.loads(gh(["api", f"repos/{repo}/{path}", "--paginate"]))
    except json.JSONDecodeError as exc:
        raise GitHubError(f"could not parse repos/{repo}/{path}: {exc}") from exc
    if not isinstance(payload, list):
        raise GitHubError(f"repos/{repo}/{path} did not return a list")

    comments: list[Comment] = []
    for item in payload:
        if not isinstance(item, dict):
            continue
        user = item.get("user")
        if not isinstance(user, dict):
            continue
        body = item.get("body")
        if not isinstance(body, str) or not body.strip():
            # An approval or a bare "LGTM" review carries no claim to read.
            continue
        has_ledger = MARKER in body
        if _is_bot(user) and not has_ledger:
            # A bot's review that carries a ledger is the previous round,
            # even when CI posted it as ``github-actions[bot]``: it is
            # never shown, but it still sets where ``--since last-round``
            # cuts. Skipping it outright would read the whole thread again.
            continue
        ident = item.get("id")
        if not isinstance(ident, int):
            continue
        created = item.get("submitted_at") or item.get("created_at")
        if not isinstance(created, str) or not created:
            continue
        login = str(user.get("login") or "")
        comments.append(
            Comment(
                id=ident,
                kind=kind,
                author=login,
                body=body,
                created_at=created,
                url=(
                    _review_url(pr_url, ident)
                    if kind == "review"
                    else str(item.get("html_url") or "")
                ),
                is_author=bool(author) and login == author,
                has_ledger=has_ledger,
                path=str(item.get("path") or ""),
                line=item.get("line") if isinstance(item.get("line"), int) else None,
                in_reply_to_id=(
                    item.get("in_reply_to_id")
                    if isinstance(item.get("in_reply_to_id"), int)
                    else None
                ),
            )
        )
    return comments


def fetch_comments(repo: str, pr: int, *, author: str | None = None) -> list[Comment]:
    """Every human comment on the PR, oldest first.

    The three surfaces are different APIs, not three views of one: a review
    body, its inline comments, and the conversation thread each carry
    different text, and the author's "fixed it" claim can land on any of
    them. Merging them here is what makes the round read the whole answer
    instead of the one an endpoint happened to return.
    """
    pull = json.loads(gh(["api", f"repos/{repo}/pulls/{pr}"]))
    if not isinstance(pull, dict):
        raise GitHubError(f"repos/{repo}/pulls/{pr} did not return an object")
    user = pull.get("user")
    login = str(user.get("login")) if isinstance(user, dict) else ""
    pr_author = author or login
    pr_url = str(pull.get("html_url") or "")

    comments: list[Comment] = []
    for path, kind in (
        (f"issues/{pr}/comments", "issue"),
        (f"pulls/{pr}/comments", "inline"),
        (f"pulls/{pr}/reviews", "review"),
    ):
        comments.extend(_collect(repo, path, kind, author=pr_author, pr_url=pr_url))

    comments.sort(key=lambda c: _timestamp(c.created_at))
    return comments


def last_round_at(comments: Iterable[Comment]) -> str | None:
    """When the newest gryphon review round was published, if any.

    The ledger is the marker, not the author: a review published anywhere
    in the thread (formal review or conversation comment) ends the previous
    round, and everything after it is new to answer.
    """
    stamped = [c.created_at for c in comments if c.has_ledger]
    return max(stamped, key=_timestamp) if stamped else None


def select(
    comments: Iterable[Comment],
    *,
    since: str = "all",
    author_only: bool = False,
    include_ledger: bool = False,
) -> list[Comment]:
    """The comments this pass should show.

    ``since="last-round"`` cuts at the newest ledger-bearing review, so a
    resumed round reads the replies to the round it is continuing rather
    than every comment since the PR opened. A thread with no ledger has no
    round to cut at, and showing everything is the honest answer there.
    """
    pool = list(comments)

    cutoff: datetime | None
    if since == "last-round":
        stamp = last_round_at(pool)
        cutoff = _parse_ts(stamp) if stamp is not None else None
    elif since in ("", "all"):
        cutoff = None
    else:
        try:
            cutoff = _parse_ts(since)
        except ValueError as exc:
            raise ValueError(
                f"--since must be 'all', 'last-round' or an ISO timestamp: {exc}"
            ) from exc

    selected = pool
    if cutoff is not None:
        selected = [c for c in selected if _timestamp(c.created_at) > cutoff]
    if author_only:
        selected = [c for c in selected if c.is_author]
    if not include_ledger:
        selected = [c for c in selected if not c.has_ledger]
    return selected


def _clip(text: str, limit: int) -> str:
    body = text.strip()
    if len(body) <= limit:
        return body
    return body[:limit].rstrip() + "\n… (truncado; o texto completo está no link)"


def render(comments: Iterable[Comment], *, pr: int | None = None) -> str:
    """The block a round shows in chat before its verdict."""
    items = list(comments)
    if not items:
        return "Nenhum comentário novo no PR."

    heading = f"## Comentários no PR #{pr}" if pr is not None else "## Comentários no PR"
    lines = [heading, ""]
    for comment in items:
        badge = " (autor)" if comment.is_author else ""
        where = f" · `{comment.path}:{comment.line}`" if comment.path else ""
        link = f" · [ver no PR]({comment.url})" if comment.url else ""
        lines.append(f"**@{comment.author}**{badge} · {comment.created_at}{where}{link}")
        for line in _clip(comment.body, BODY_LIMIT).splitlines():
            lines.append(f"> {line}".rstrip())
        lines.append("")
    return "\n".join(lines).rstrip() + "\n"


def _reaction_path(repo: str, pr: int, comment: Comment) -> str:
    if comment.kind == "issue":
        return f"repos/{repo}/issues/comments/{comment.id}/reactions"
    if comment.kind == "inline":
        return f"repos/{repo}/pulls/comments/{comment.id}/reactions"
    return f"repos/{repo}/pulls/{pr}/reviews/{comment.id}/reactions"


def react(
    repo: str,
    pr: int,
    comments: Iterable[Comment],
    content: str,
    *,
    dry_run: bool = False,
) -> list[dict[str, object]]:
    """Put one reaction on each comment. Idempotent on GitHub's side."""
    if content not in REACTIONS:
        raise ValueError(f"reaction {content!r} is not one of {REACTIONS}")

    actions: list[dict[str, object]] = []
    for comment in comments:
        if not dry_run:
            gh(
                [
                    "api",
                    _reaction_path(repo, pr, comment),
                    "-X",
                    "POST",
                    "-f",
                    f"content={content}",
                ]
            )
        actions.append(
            {
                "action": "react",
                "id": comment.id,
                "kind": comment.kind,
                "author": comment.author,
                "reaction": content,
                "dry_run": dry_run,
            }
        )
    return actions


def reply(
    repo: str,
    pr: int,
    comment: Comment,
    body: str,
    *,
    dry_run: bool = False,
) -> dict[str, object]:
    """Answer one comment in the place the author will look.

    Inline review comments support a threaded reply, so the answer stays
    under the finding. A conversation comment and a formal review do not,
    so the answer is posted as a new conversation comment quoting the
    original and mentioning its author — otherwise it reads as an unrelated
    remark in the middle of the thread.
    """
    text = body.strip()
    if not text:
        raise ValueError("the reply is empty")

    if comment.kind == "inline":
        path = f"repos/{repo}/pulls/{pr}/comments/{comment.id}/replies"
        payload = text
    else:
        quoted = "\n".join(
            f"> {line}" for line in _clip(comment.body, QUOTE_LIMIT).splitlines()
        )
        path = f"repos/{repo}/issues/{pr}/comments"
        payload = f"{quoted}\n\n@{comment.author} {text}"

    if not dry_run:
        gh(["api", path, "-X", "POST", "-f", f"body={payload}"])

    return {
        "action": "reply",
        "id": comment.id,
        "kind": comment.kind,
        "author": comment.author,
        "dry_run": dry_run,
    }


def _configure_stdout() -> None:
    """Write the thread as UTF-8, whatever the console code page is.

    A PR comment is arbitrary text: an arrow, an emoji or an accented word
    is enough to make ``print`` raise ``UnicodeEncodeError`` on a Windows
    console stuck on cp1252. The alternative is a review that dies while
    showing the author's reply, which is the one moment it must not.
    """
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[attr-defined]
    except (AttributeError, ValueError):
        pass


def _main(argv: list[str] | None = None) -> int:
    _configure_stdout()
    parser = argparse.ArgumentParser(
        description="Read a PR's developer comments and react to them."
    )
    parser.add_argument("--repo", required=True, help="owner/name")
    parser.add_argument("--pr", required=True, type=int)
    parser.add_argument(
        "--since",
        default="all",
        help=(
            "Show comments after 'last-round' (the newest ledger in the thread), "
            "an ISO timestamp, or 'all' (default)"
        ),
    )
    parser.add_argument(
        "--author-only",
        action="store_true",
        help="Only the PR author's comments (default: every human)",
    )
    parser.add_argument(
        "--include-ledger",
        action="store_true",
        help="Include the review bodies that carry a state ledger",
    )
    parser.add_argument(
        "--react",
        choices=REACTIONS,
        help="React to each selected comment with this content",
    )
    parser.add_argument(
        "--reply-id",
        type=int,
        help="Comment id to answer with --reply-text",
    )
    parser.add_argument("--reply-text", help="The answer to post")
    parser.add_argument("--reply-file", type=Path, help="Read the answer from a file")
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Print the plan without reacting or posting",
    )
    parser.add_argument("--json", action="store_true", help="Emit JSON on stdout")
    args = parser.parse_args(argv)

    logging.basicConfig(level=logging.WARNING, format="%(levelname)s: %(message)s")

    if args.reply_id is not None and not (args.reply_text or args.reply_file):
        logger.error("--reply-id needs --reply-text or --reply-file")
        return 3
    if args.reply_id is None and (args.reply_text or args.reply_file):
        logger.error("--reply-text/--reply-file need --reply-id")
        return 3
    if args.reply_text and args.reply_file:
        logger.error("use --reply-text or --reply-file, not both")
        return 3

    if args.since not in ("", "all", "last-round"):
        try:
            _parse_ts(args.since)
        except ValueError as exc:
            logger.error("--since is not an ISO timestamp: %s", exc)
            return 3

    try:
        since = args.since
        comments = fetch_comments(args.repo, args.pr)
        selected = select(
            comments,
            since=since,
            author_only=args.author_only,
            include_ledger=args.include_ledger,
        )
    except ValueError as exc:
        logger.error("%s", exc)
        return 3
    except GitHubError as exc:
        logger.error("%s", exc)
        return 2

    actions: list[dict[str, object]] = []
    try:
        if args.react:
            actions.extend(
                react(args.repo, args.pr, selected, args.react, dry_run=args.dry_run)
            )
        if args.reply_id is not None:
            target = next((c for c in comments if c.id == args.reply_id), None)
            if target is None:
                logger.error("no comment with id %s on %s#%s", args.reply_id, args.repo, args.pr)
                return 3
            body = args.reply_text
            if args.reply_file is not None:
                try:
                    body = args.reply_file.read_text(encoding="utf-8-sig")
                except OSError as exc:
                    logger.error("could not read the reply: %s", exc)
                    return 3
            actions.append(
                reply(args.repo, args.pr, target, body or "", dry_run=args.dry_run)
            )
    except ValueError as exc:
        logger.error("%s", exc)
        return 3
    except GitHubError as exc:
        logger.error("%s", exc)
        return 2

    if args.json:
        print(
            json.dumps(
                {"comments": [c.as_dict() for c in selected], "actions": actions},
                indent=2,
                ensure_ascii=False,
            )
        )
    else:
        print(render(selected, pr=args.pr))
        for action in actions:
            verb = "would react with" if action["dry_run"] else "reacted with"
            if action["action"] == "reply":
                verb = "would reply to" if action["dry_run"] else "replied to"
                print(f"{verb} {action['kind']} comment {action['id']} by @{action['author']}")
            else:
                print(
                    f"{verb} {action['reaction']} on {action['kind']} comment "
                    f"{action['id']} by @{action['author']}"
                )
    return 0


if __name__ == "__main__":
    sys.exit(_main())
