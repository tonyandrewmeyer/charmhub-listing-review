#! /usr/bin/env python3

# /// script
# dependencies = []
# ///

# Copyright 2026 Canonical Ltd.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

"""Post reminder comments on stalled charm listing review issues.

Intended to run on a schedule (e.g. daily). For each open listing-request
issue, decides whether the review has stalled since the last human activity
and, if so, posts a reminder pinging the current assignees. If a review has
been stalled twice as long as the reminder threshold, the reminder also
pings the charm-tech team.

Reminders left by this script are marked with an HTML comment so that later
runs can tell "the last activity was one of our own reminders" apart from
"the last activity was a real human comment", and count idle time from the
earlier of the two.
"""

from __future__ import annotations

import argparse
import datetime
import json
import subprocess  # noqa: S404
import sys
from typing import TypedDict

REMINDER_MARKER = '<!-- charmhub-listing-review-reminder -->'
ESCALATION_TEAM = '@canonical/charm-tech'
DEFAULT_REMIND_AFTER_DAYS = 7
DEFAULT_ESCALATE_AFTER_DAYS = 14


class _IssueComment(TypedDict):
    """Subset of the `gh issue view --json comments` schema we consume."""

    author: dict[str, str]
    body: str
    createdAt: str


class _Issue(TypedDict):
    """Subset of the `gh issue list --json ...` schema we consume."""

    number: int
    title: str
    assignees: list[dict[str, str]]
    updatedAt: str
    createdAt: str


def _run_gh(args: list[str], repo: str | None = None) -> str:
    """Run a `gh` invocation and return its stdout."""
    cmd = ['gh', *args]
    if repo:
        cmd.extend(['--repo', repo])
    result = subprocess.run(cmd, capture_output=True, text=True, check=True)
    return result.stdout


def list_open_listing_requests(repo: str | None = None) -> list[_Issue]:
    """Return open issues carrying the `listing-request` label."""
    stdout = _run_gh(
        [
            'issue',
            'list',
            '--state',
            'open',
            '--label',
            'listing-request',
            '--limit',
            '200',
            '--json',
            'number,title,assignees,updatedAt,createdAt',
        ],
        repo=repo,
    )
    return json.loads(stdout)


def get_issue_comments(issue_number: int, repo: str | None = None) -> list[_IssueComment]:
    """Return comments on the issue, oldest first."""
    stdout = _run_gh(
        [
            'issue',
            'view',
            str(issue_number),
            '--json',
            'comments',
        ],
        repo=repo,
    )
    return json.loads(stdout).get('comments', [])


def _parse_iso(timestamp: str) -> datetime.datetime:
    """Parse an ISO 8601 timestamp from the GitHub API into an aware datetime.

    The API returns timestamps with a trailing `Z`, which `fromisoformat`
    accepts natively from Python 3.11 onwards; we already require 3.12.
    """
    return datetime.datetime.fromisoformat(timestamp)


def _is_bot_reminder(comment: _IssueComment) -> bool:
    """Return True if a comment was posted by an earlier run of this script."""
    return REMINDER_MARKER in comment.get('body', '')


def days_since_last_human_activity(
    issue: _Issue,
    comments: list[_IssueComment],
    *,
    now: datetime.datetime,
) -> float:
    """Return whole and fractional days since the last non-reminder activity.

    "Activity" means either the issue being (re)opened or a comment that was
    not itself posted by this reminder script. Reminders we posted ourselves
    don't restart the clock — otherwise every reminder would silence itself
    for another full window.
    """
    last = _parse_iso(issue['createdAt'])
    for comment in comments:
        if _is_bot_reminder(comment):
            continue
        created = _parse_iso(comment['createdAt'])
        if created > last:
            last = created
    return (now - last).total_seconds() / 86400.0


def hours_since_last_reminder(
    comments: list[_IssueComment],
    *,
    now: datetime.datetime,
) -> float | None:
    """Return hours since the most recent reminder, or None if there is none.

    Used to avoid posting multiple reminders per day when the schedule fires
    more often than the reminder cadence, or when a run is retried.
    """
    latest: datetime.datetime | None = None
    for comment in comments:
        if not _is_bot_reminder(comment):
            continue
        created = _parse_iso(comment['createdAt'])
        if latest is None or created > latest:
            latest = created
    if latest is None:
        return None
    return (now - latest).total_seconds() / 3600.0


def _format_ping(assignees: list[dict[str, str]]) -> str:
    """Return a `@name @name` string for the current assignees, or a fallback."""
    names = [a['login'] for a in assignees if a.get('login')]
    if not names:
        return 'Reviewer (no assignee)'
    return ' '.join(f'@{n}' for n in names)


def format_reminder(
    assignees: list[dict[str, str]],
    idle_days: float,
    *,
    escalate: bool,
) -> str:
    """Build the reminder comment body."""
    ping = _format_ping(assignees)
    idle_int = int(idle_days)
    if escalate:
        return (
            f'{REMINDER_MARKER}\n'
            f'{ping} — this listing review has had no activity for '
            f'{idle_int} days. Escalating to {ESCALATION_TEAM}.\n\n'
            f'If the assigned reviewer is unavailable, please help find a '
            f'replacement so the author is not left waiting. See the '
            f'[README](../README.md) for the review process.'
        )
    return (
        f'{REMINDER_MARKER}\n'
        f'{ping} — this listing review has had no activity for '
        f'{idle_int} days. Please post an update, delegate to a reviewer, '
        f'or ask the author for the next thing you need.\n\n'
        f"If this review is stuck for a reason that isn't obvious from the "
        f'thread, a short note explaining the state will keep the author in '
        f'the loop.'
    )


def post_comment(issue_number: int, body: str, repo: str | None = None) -> None:
    """Post `body` as a new comment on the issue."""
    _run_gh(
        [
            'issue',
            'comment',
            str(issue_number),
            '--body',
            body,
        ],
        repo=repo,
    )


def decide_and_post(
    issue: _Issue,
    comments: list[_IssueComment],
    *,
    now: datetime.datetime,
    remind_after_days: int,
    escalate_after_days: int,
    min_hours_between_reminders: float,
    dry_run: bool,
    repo: str | None,
) -> str:
    """Return a short status string describing the decision made for one issue.

    The status is suitable for printing from the CLI (`skipped: …`,
    `reminded: …`, `escalated: …`) and doubles as the return value that the
    tests assert against.
    """
    idle = days_since_last_human_activity(issue, comments, now=now)
    if idle < remind_after_days:
        return f'skipped (idle {idle:.1f}d < {remind_after_days}d)'

    hours_since_last = hours_since_last_reminder(comments, now=now)
    if hours_since_last is not None and hours_since_last < min_hours_between_reminders:
        return (
            f'skipped (reminder posted {hours_since_last:.1f}h ago, '
            f'< {min_hours_between_reminders:.0f}h floor)'
        )

    escalate = idle >= escalate_after_days
    body = format_reminder(issue['assignees'], idle, escalate=escalate)

    if dry_run:
        print(f'--- issue #{issue["number"]} (dry run) ---')
        print(body)
        return f'{"escalated" if escalate else "reminded"} (dry run, idle {idle:.1f}d)'

    post_comment(issue['number'], body, repo=repo)
    return f'{"escalated" if escalate else "reminded"} (idle {idle:.1f}d)'


def main(argv: list[str] | None = None) -> int:
    """Entry point: iterate open listing-request issues and remind where stale."""
    parser = argparse.ArgumentParser(
        description=(
            'Post reminder comments on stalled charm listing review issues. '
            'Reminders start after the --remind-after threshold and escalate '
            'to the charm-tech team after --escalate-after.'
        )
    )
    parser.add_argument(
        '--repo',
        help='GitHub repository in OWNER/REPO format. Defaults to the current one.',
    )
    parser.add_argument(
        '--remind-after',
        type=int,
        default=DEFAULT_REMIND_AFTER_DAYS,
        help=f'Days without activity before the first reminder '
        f'(default: {DEFAULT_REMIND_AFTER_DAYS}).',
    )
    parser.add_argument(
        '--escalate-after',
        type=int,
        default=DEFAULT_ESCALATE_AFTER_DAYS,
        help=f'Days without activity before the reminder escalates to '
        f'{ESCALATION_TEAM} (default: {DEFAULT_ESCALATE_AFTER_DAYS}).',
    )
    parser.add_argument(
        '--min-hours-between-reminders',
        type=float,
        default=20.0,
        help='Minimum hours between reminders on the same issue (default: 20). '
        'Keeps a retried or extra-frequent schedule from stacking reminders.',
    )
    parser.add_argument(
        '--dry-run',
        action='store_true',
        help='Print what would be posted without touching the issues.',
    )
    args = parser.parse_args(argv)

    if args.escalate_after < args.remind_after:
        parser.error('--escalate-after must be >= --remind-after')

    now = datetime.datetime.now(tz=datetime.UTC)
    issues = list_open_listing_requests(repo=args.repo)
    if not issues:
        print('No open listing-request issues found.')
        return 0

    exit_code = 0
    for issue in issues:
        try:
            comments = get_issue_comments(issue['number'], repo=args.repo)
            status = decide_and_post(
                issue,
                comments,
                now=now,
                remind_after_days=args.remind_after,
                escalate_after_days=args.escalate_after,
                min_hours_between_reminders=args.min_hours_between_reminders,
                dry_run=args.dry_run,
                repo=args.repo,
            )
        except subprocess.CalledProcessError as e:
            print(
                f'#{issue["number"]}: gh call failed: {e.stderr.strip() or e}',
                file=sys.stderr,
            )
            exit_code = 1
            continue
        print(f'#{issue["number"]}: {status}')

    return exit_code


if __name__ == '__main__':
    sys.exit(main())
