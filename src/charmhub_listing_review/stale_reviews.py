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

"""Close listing request issues that have gone stale.

A listing request issue sometimes stalls: a review is done, and then weeks or
months go by without the author following up. This tool finds open
`listing-request` issues with no recent activity, warns on the issue, and
closes it a couple of days later if the warning went unanswered.
"""

import argparse
import datetime
import json
import subprocess  # ruff: ignore[suspicious-subprocess-import]

LISTING_REQUEST_LABEL = 'listing-request'
STALE_LABEL = 'stale'

# The first line of the warning comment, used on later runs to recognise that
# the most recent comment on the issue *is* our own warning (as opposed to a
# reply from the author or reviewer, which means the issue is active again).
STALE_WARNING_MARKER = '**This issue has had no activity for a while.**'

STALE_WARNING_COMMENT = f"""\
{STALE_WARNING_MARKER}

If there is no further activity within a couple of days, it will be closed.
If you're still working on this, or waiting on something, just leave a
comment (or otherwise update the issue) and it will stay open.
"""

STALE_CLOSE_COMMENT = (
    'Closing due to inactivity after the warning above. Please reopen '
    '(or open a new listing request) when you want to pick this back up.'
)


def _parse_timestamp(value: str) -> datetime.datetime:
    """Parse a GitHub API timestamp (e.g. "2026-01-01T12:34:56Z")."""
    return datetime.datetime.fromisoformat(value.replace('Z', '+00:00'))


def list_open_listing_requests(repo: str | None = None) -> list[dict]:
    """List open issues with the "listing-request" label.

    Requires `gh` CLI to be installed and authenticated.
    """
    cmd = [
        'gh',
        'issue',
        'list',
        '--label',
        LISTING_REQUEST_LABEL,
        '--state',
        'open',
        '--json',
        'number,labels,updatedAt',
        '--limit',
        '200',
    ]
    if repo:
        cmd.extend(['--repo', repo])
    result = subprocess.run(cmd, capture_output=True, text=True, check=True)
    return json.loads(result.stdout)


def get_issue_comments(issue_number: int, repo: str | None = None) -> list[dict]:
    """Fetch the comments (oldest first) for the given issue."""
    cmd = ['gh', 'issue', 'view', str(issue_number), '--json', 'comments']
    if repo:
        cmd.extend(['--repo', repo])
    result = subprocess.run(cmd, capture_output=True, text=True, check=True)
    return json.loads(result.stdout).get('comments', [])


def _last_activity_at(issue: dict, comments: list[dict]) -> datetime.datetime:
    """Return the timestamp of the most recent comment, or the issue's own last update."""
    if comments:
        return _parse_timestamp(comments[-1]['createdAt'])
    return _parse_timestamp(issue['updatedAt'])


def warn_issue(issue_number: int, repo: str | None, dry_run: bool) -> None:
    """Post the stale warning comment and label the issue as stale."""
    if dry_run:
        print(f'#{issue_number}: would post stale warning and add "{STALE_LABEL}" label')
        return
    cmd = ['gh', 'issue', 'comment', str(issue_number), '--body', STALE_WARNING_COMMENT]
    if repo:
        cmd.extend(['--repo', repo])
    subprocess.run(cmd, check=True)
    cmd = ['gh', 'issue', 'edit', str(issue_number), '--add-label', STALE_LABEL]
    if repo:
        cmd.extend(['--repo', repo])
    subprocess.run(cmd, check=True)


def unwarn_issue(issue_number: int, repo: str | None, dry_run: bool) -> None:
    """Remove the stale label, since there has been activity since the warning."""
    if dry_run:
        print(f'#{issue_number}: would remove "{STALE_LABEL}" label (activity since warning)')
        return
    cmd = ['gh', 'issue', 'edit', str(issue_number), '--remove-label', STALE_LABEL]
    if repo:
        cmd.extend(['--repo', repo])
    subprocess.run(cmd, check=True)


def close_stale_issue(issue_number: int, repo: str | None, dry_run: bool) -> None:
    """Close the issue, since the stale warning went unanswered."""
    if dry_run:
        print(f'#{issue_number}: would close as stale')
        return
    cmd = ['gh', 'issue', 'close', str(issue_number), '--comment', STALE_CLOSE_COMMENT]
    if repo:
        cmd.extend(['--repo', repo])
    subprocess.run(cmd, check=True)


def process_issue(
    issue: dict,
    repo: str | None,
    now: datetime.datetime,
    warn_after: datetime.timedelta,
    close_after: datetime.timedelta,
    dry_run: bool,
) -> str:
    """Warn on, close, or leave alone a single issue, depending on its activity.

    Returns one of "warned", "closed", "reactivated", or "active", describing
    the action taken.
    """
    number = issue['number']
    labels = {label['name'] for label in issue['labels']}
    comments = get_issue_comments(number, repo)

    if STALE_LABEL not in labels:
        if now - _last_activity_at(issue, comments) >= warn_after:
            warn_issue(number, repo, dry_run)
            return 'warned'
        return 'active'

    # Already warned: close if our warning is still the last comment and enough
    # time has passed, otherwise someone responded, so clear the stale label.
    last_comment = comments[-1] if comments else None
    if last_comment is not None and last_comment['body'].startswith(STALE_WARNING_MARKER):
        if now - _parse_timestamp(last_comment['createdAt']) >= close_after:
            close_stale_issue(number, repo, dry_run)
            return 'closed'
        return 'active'

    unwarn_issue(number, repo, dry_run)
    return 'reactivated'


def close_stale_requests(
    repo: str | None = None,
    warn_after_days: int = 28,
    close_after_days: int = 2,
    dry_run: bool = False,
) -> dict[str, list[int]]:
    """Warn on and close stale listing-request issues.

    Returns a mapping of action ("warned", "closed", "reactivated", "active")
    to the list of issue numbers that received that action.
    """
    now = datetime.datetime.now(datetime.UTC)
    warn_after = datetime.timedelta(days=warn_after_days)
    close_after = datetime.timedelta(days=close_after_days)

    results: dict[str, list[int]] = {'warned': [], 'closed': [], 'reactivated': [], 'active': []}
    for issue in list_open_listing_requests(repo):
        action = process_issue(issue, repo, now, warn_after, close_after, dry_run)
        results[action].append(issue['number'])
    return results


def main():
    """Warn on and close listing-request issues that have gone stale."""
    parser = argparse.ArgumentParser(
        description='Close listing request issues that have had no activity for a while.'
    )
    parser.add_argument(
        '--repo',
        type=str,
        help='GitHub repository in OWNER/REPO format (e.g. canonical/charmhub-listing-review)',
    )
    parser.add_argument(
        '--warn-after-days',
        type=int,
        default=28,
        help='days of inactivity before warning on an issue (default: %(default)s)',
    )
    parser.add_argument(
        '--close-after-days',
        type=int,
        default=2,
        help='days after the warning before closing the issue (default: %(default)s)',
    )
    parser.add_argument(
        '--dry-run', action='store_true', help='do not modify any issue, just print the actions'
    )
    args = parser.parse_args()

    results = close_stale_requests(
        repo=args.repo,
        warn_after_days=args.warn_after_days,
        close_after_days=args.close_after_days,
        dry_run=args.dry_run,
    )
    for action, numbers in results.items():
        if numbers:
            print(f'{action}: {", ".join(f"#{n}" for n in numbers)}')


if __name__ == '__main__':
    main()
