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

"""Check the review checklist when a listing request issue is closed as completed.

If the reviewer's checklist is not fully ticked, the issue is reopened with a
comment listing what is missing. If it is complete, the issue is labelled so
that the store team can be asked to switch the listing to public.
"""

import argparse
import json
import re
import subprocess  # ruff: ignore[suspicious-subprocess-import]

READY_LABEL = 'ready-for-store-team'
CHECKLIST_HEADING = '## Listing requirements'
# The icon is explicitly optional, so it never blocks listing.
OPTIONAL_MARKER = '(recommended)'
_ITEM = re.compile(r'^\s*[*-] \[(?P<mark>[ xX])\] (?P<text>.+)$')


def find_reviewer_checklist(comments: list[dict]) -> str | None:
    """Return the body of the reviewer's checklist comment, if there is one.

    The comment posted by the automation holds the template inside a collapsed
    ``<details>`` block, so it is skipped. The reviewer's copy is the latest
    other comment that contains the checklist heading.
    """
    for comment in reversed(comments):
        body = comment.get('body', '')
        if CHECKLIST_HEADING in body and '<details>' not in body:
            return body
    return None


def unticked_items(checklist: str) -> list[str]:
    """Return the text of each required item in the checklist that is not ticked."""
    missing: list[str] = []
    for line in checklist.splitlines():
        match = _ITEM.match(line)
        if match and match['mark'] == ' ' and OPTIONAL_MARKER not in match['text']:
            missing.append(match['text'])
    return missing


def _gh(args: list[str], repo: str | None) -> subprocess.CompletedProcess[str]:
    cmd = ['gh', *args]
    if repo:
        cmd.extend(['--repo', repo])
    return subprocess.run(cmd, capture_output=True, text=True, check=True)


def check_closed_issue(issue_number: int, repo: str | None = None, dry_run: bool = False):
    """Reopen the issue if the checklist is incomplete, otherwise mark it ready."""
    number = str(issue_number)
    view = _gh(['issue', 'view', number, '--json', 'comments'], repo)
    checklist = find_reviewer_checklist(json.loads(view.stdout).get('comments', []))
    if checklist is None:
        missing = ['The reviewer has not posted the listing requirements checklist.']
    else:
        missing = unticked_items(checklist)

    if missing:
        body = (
            'This issue was closed as completed, but the review checklist is not '
            'complete, so it has been reopened. Items still to be checked:\n\n'
            + '\n'.join(f'* {item}' for item in missing)
        )
        commands = [
            ['issue', 'reopen', number],
            ['issue', 'comment', number, '--body', body],
        ]
    else:
        body = (
            'The review checklist is complete and the request is closed. The '
            'store team can now switch the listing to public.'
        )
        commands = [
            ['issue', 'edit', number, '--add-label', READY_LABEL],
            ['issue', 'comment', number, '--body', body],
        ]
    for args in commands:
        if dry_run:
            print(' '.join(args))
        else:
            _gh(args, repo)


def main():
    """Check the checklist of a listing request issue that has just been closed."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--issue-number', type=int, required=True)
    parser.add_argument('--repo', type=str, help='GitHub repository in OWNER/REPO format')
    parser.add_argument('--dry-run', action='store_true')
    args = parser.parse_args()
    check_closed_issue(args.issue_number, repo=args.repo, dry_run=args.dry_run)


if __name__ == '__main__':
    main()
