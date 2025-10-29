#! /usr/bin/env python3

# /// script
# dependencies = [
#   "pyyaml",
#   "requests"
# ]
# ///

# Copyright 2025 Canonical Ltd.
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

"""Update a GitHub issue for a charm listing review with the current checklist.

Issues for listing review requests are created by the usual GitHub issue
creation process, with the help of the issue template. This script updates the
created issue to be user-friendly, and to include the current checklist.
"""

import argparse
import json
import pathlib
import random
import re
import subprocess  # noqa: S404
import urllib.error
import urllib.request
from typing import TypedDict, cast

import yaml

from .evaluate import evaluate

BEST_PRACTICE_SOURCE = 'https://raw.githubusercontent.com/canonical/operator/refs/heads/main/docs/reuse/best-practices.txt'


def issue_summary(name: str):
    """Provide a suitable issue title."""
    return f'Review `{name}` for public listing on Charmhub'


def issue_comment(
    name: str,
    demo_url: str,
    ci_release_url: str,
    ci_integration_url: str,
    documentation_link: str,
):
    """Provide a suitable issue comment.

    The comment outlines what is required by the reviewer. It will pre-tick any
    of the items that can be automatically checked, as they are at the time of
    the initial comment.
    """
    # fmt: off
    description = [
        f"""
Reviewer: Please review the charm, **within three working days** by checking each of the items in the following checklist. If blocking issues are found, please help the author work through those, and respond to any follow-up posts within *one working day*.

If you find other improvements or fixes that could be made to the charm, feel free to suggest those, but **they do not block listing**. If you find something that's missing from the review checklist or best practices, please separately suggest that (see [CONTRIBUTING.md](../CONTRIBUTING.md)) so that we can keep the review process consistent.

See the [README](../README.md) for more information about the charm listing review process.

When reviewing test coverage of the charm, note that:

* Unit tests are recommended, but *not* required.
* A minimal set of integration tests is required, as outlined in the checklist.
* There is no minimum for test coverage. We suggest that tests cover at least all configuration options and actions, as well as the observed Juju events, but this is not a requirement for listing.
* Some charms may have additional tests in an external location, particularly if the charm has specific resource requirements (such as specific hardware).

<details>
<summary>Reviewer: copy this list of requirements to a separate comment, so you can check things off</summary>

```
## Listing requirements

* [ ] The charm does what it is meant to do, per the [demo or tutorial]({demo_url}).
* [ ] The [charm's page on Charmhub](https://charmhub.io/{name}) provides a quality impression. The overall appearance looks good and the [documentation]({documentation_link}) looks reasonable.
* [ ] The charm has an icon.
* [ ] [Automated releasing]({ci_release_url}) to unstable channels exists
* [ ] [Integration tests]({ci_integration_url}) exist, are run on every change to the default branch, and are passing. At minimum, the tests verify that the charm can be deployed and ends up in a success state, and that the charm can be integrated with at least one example for each 'provides' and 'requires' specified (including optional, excluding tracing) ending up in a success state. The tests should be run with `charmcraft test`.
""".strip()  # noqa: E501
    ]
    description.append('\n\n')
    description.append(
        """
### Documentation

A charm's documentation should focus on the charm itself. For workload-specific or Juju-related content, link to the appropriate upstream documentation. A smaller charm can have single-page documentation for its description. A bigger charm should include a full Diátaxis navigation tree. Check that the charm has documentation that covers:
* [ ] How to use the charm, including configuration, limitations, and deviations in behaviour from the “non-charmed” version of the application.
* [ ] How to modify the charm
* [ ] A concise summary of the charm in the `charmcraft.yaml` 'summary' field, and a more detailed description in the `charmcraft.yaml` 'description' field.
""".strip(),  # noqa: E501
    )

    # fmt: on
    try:
        with urllib.request.urlopen(BEST_PRACTICE_SOURCE) as response:  # noqa: S310
            best_practices = response.read().decode().splitlines()
    except (urllib.error.URLError, urllib.error.HTTPError):
        best_practices = []
    # Remove the headings and empty lines.
    best_practices = [line for line in best_practices if line.startswith('-')]
    if best_practices:
        description.append('\n\n')
        description.append(
            f"""
### Best practices

The following best practices are recommended for all charms, and are also
required for listing.

{'\n'.join(('* [ ] ' + p.removeprefix('- ')) for p in best_practices)}
""".strip()
        )

    description.append('\n```\n</details>\n')

    return ''.join(description)


class _IssueData(TypedDict):
    """Typed dictionary for issue data."""

    name: str
    demo_url: str
    project_repo: str
    ci_linting: str
    ci_release_url: str
    ci_integration_url: str
    documentation_link: str
    contribution_link: str
    license_link: str
    security_link: str


def get_details_from_issue(issue_number: int):
    """Fetch details from the issue number using the GitHub CLI.

    Requires `gh` CLI to be installed and authenticated.
    """
    result = subprocess.run(
        ['gh', 'issue', 'view', str(issue_number), '--json', 'body'],
        capture_output=True,
        text=True,
        check=True,
    )
    body = json.loads(result.stdout)['body']

    # Define the fields to extract and their headings.
    fields = {
        'name': '### Charm name',
        'demo_url': '### Demo',
        'project_repo': '### Project Repository',
        'ci_linting': '### CI Linting',
        'ci_release_url': '### CI Release',
        'ci_integration_url': '### CI Integration Tests',
        'documentation_link': '### Documentation Link',
    }

    # Extract values for each field.
    issue_data: dict[str, bool | str | None] = {}
    for key, heading in fields.items():
        pattern = rf'{re.escape(heading)}\s*\n([^\n]*)'
        match = re.search(pattern, body)
        if match:
            value = match.group(1).strip()
            issue_data[key] = value
        else:
            issue_data[key] = None

    # These have expected filenames, so we use those rather than require the author provide them.
    # This is quite specific to GitHub, but we can add support for other platforms if required,
    # and if they aren't found then the reviewer just has to locate them themselves.
    issue_data['contribution_link'] = f'{issue_data["project_repo"]}/blob/main/CONTRIBUTING.md'
    issue_data['license_link'] = f'{issue_data["project_repo"]}/blob/main/LICENSE'
    issue_data['security_link'] = f'{issue_data["project_repo"]}/blob/main/SECURITY.md'

    return cast('_IssueData', issue_data)


def assign_review(issue_number: int, dry_run: bool = False):
    """Assign the issue to a team.

    We assign the issue to a single person (generally the manager) from a
    charming team. The expectation is that they will then delegate the actual
    review to a member of their team. The manager has overall responsibility for
    ensuring that the review is completed on time (with Charm Tech responsible
    for keeping the manager accountable).

    The manager can't assign the issue directly to a reviewer (without having
    every possible reviewer added as a collaborator on the repository), so they
    are expected to simply ping them in a comment. Once they have submitted
    their review, the author can interact with them in the usual way.
    """
    # TODO: Figure out where this should be and how the script should locate it.
    reviewers_file = pathlib.Path(
        '/home/runner/work/charmhub-listing-review/charmhub-listing-review/reviewers.yaml'
    )
    with reviewers_file.open('r') as f:
        reviewers_data = yaml.safe_load(f)
    reviewers = reviewers_data['reviewers']
    teams = [info['team'] for info in reviewers.values()]
    team = random.choice(teams)  # noqa: S311
    # If there happen to be multiple people in a team, then randomly pick among
    # them.
    team_reviewers = [name for name, info in reviewers.items() if info['team'] == team]
    reviewer = random.choice(team_reviewers)  # noqa: S311

    if not dry_run:
        subprocess.run(
            ['gh', 'issue', 'edit', str(issue_number), '--add-assignee', reviewer[1:]],
            check=True,
        )
    return reviewer


def update_gh_issue(issue_number: int, summary: str, comment: str, dry_run: bool = False):
    """Update the specified GitHub issue with the latest generated comment."""
    # Update the issue title.
    if dry_run:
        print(summary)
        print()
    else:
        subprocess.run(
            ['gh', 'issue', 'edit', str(issue_number), '--title', summary],
            check=True,
        )

    # Assign the issue, if it is not already.
    gh = subprocess.run(
        ['gh', 'issue', 'view', str(issue_number), '--json', 'assignees'],
        capture_output=True,
        text=True,
    )
    assignees = json.loads(gh.stdout.strip()).get('assignees', [])
    manager = assignees[0]['login'] if assignees else assign_review(issue_number, dry_run)
    request_review = re.sub(
        r'\s',
        ' ',
        f"""\
{manager} - please assign this review to someone in your team, and mention
their name in a comment (for example, "Hi @canonical-person, please review this
charm"). Please choose someone that will have time to complete the initial
review within the next three working days.
""",
    )
    comment = f'{request_review}\n\n{comment}'

    existing_comments = subprocess.run(
        ['gh', 'issue', 'view', str(issue_number), '--json', 'comments'],
        capture_output=True,
        text=True,
        check=True,
    )
    existing_comments = json.loads(existing_comments.stdout.strip()).get('comments', [])
    if not existing_comments:
        # Create a new comment.
        if dry_run:
            print(comment)
        else:
            subprocess.run(
                ['gh', 'issue', 'comment', str(issue_number), '--body', comment],
                check=True,
            )
        return

    # Update the first comment with the new content.
    if dry_run:
        print(comment)
    else:
        subprocess.run(
            [
                'gh',
                'issue',
                'comment',
                str(issue_number),
                '--edit-last',  # comment of the current user
                '--body',
                comment,
            ],
            check=True,
        )


def apply_automated_checks(issue_data: _IssueData, comment: str):
    """Adjust the comment to tick items based on automated checks."""
    results = evaluate(
        issue_data['name'],
        issue_data['project_repo'],
        issue_data['ci_linting'],
        issue_data['contribution_link'],
        issue_data['license_link'],
        issue_data['security_link'],
    )
    for result in results:
        if result.replace('* [x]', '* [ ]') in comment:
            comment = comment.replace(result.replace('* [x]', '* [ ]'), result)
    return comment


def main():
    """Extract information from the issue and post/update a review comment."""
    parser = argparse.ArgumentParser(
        description='Update a GitHub issue for a charm listing review.'
    )
    parser.add_argument(
        '--issue-number', type=int, help='The issue number to update', required=True
    )
    parser.add_argument(
        '--dry-run', action='store_true', help='Do not update the issue, just print the output'
    )
    args = parser.parse_args()

    issue_data = get_details_from_issue(args.issue_number)

    summary = issue_summary(issue_data['name'])
    comment = issue_comment(
        issue_data['name'],
        issue_data['demo_url'],
        issue_data['ci_release_url'],
        issue_data['ci_integration_url'],
        issue_data['documentation_link'],
    )
    comment = apply_automated_checks(issue_data, comment)

    update_gh_issue(args.issue_number, summary, comment, dry_run=args.dry_run)


if __name__ == '__main__':
    main()
