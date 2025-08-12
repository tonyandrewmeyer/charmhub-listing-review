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
from typing import TypedDict, cast

import yaml

import evaluate

BEST_PRACTICE_RE_MD = re.compile(
    r'```{admonition} Best practice\s*(?:.*?\n)?([\s\S]*?)```',
    re.MULTILINE,
)
BEST_PRACTICE_RE_REST = re.compile(
    r'^\.\. admonition:: Best practice\s*\n\s*:class: hint\s*\n\s*\n([\s\S]*?)(?=\n\.\. |\n\n|\Z)',
    re.MULTILINE,
)


def issue_summary(name: str):
    """Provide a suitable issue title."""
    return f'Review `{name}` for public listing on Charmhub'


def issue_comment(
    name: str,
    demo_url: str,
    ci_release_url: str,
    ci_integration_url: str,
    documentation_link: str,
    path_to_ops: pathlib.Path,
    path_to_charmcraft: pathlib.Path,
):
    """Provide a suitable issue comment.

    The comment outlines what is required by the reviewer and what the current
    progress against the review is.
    """
    # fmt: off
    description = [
        f"""
Please review the charm by checking each of the items in the following checklist.

If you find other improvements or fixes that could be made to the charm, feel free to suggest those, but **they do not block listing**. If you find something that's missing from the review checklist or best practices, please separately suggest that (see [CONTRIBUTING.md](../CONTRIBUTING.md)) so that we can keep the review process consistent.

See the [README](../README.md) for more information about the charm listing review process.

When reviewing test coverage of the charm, note that:

* Unit tests are recommended, but *not* required.
* A minimal set of integration tests is required, as outlined in the checklist.
* There is no minimum for test coverage. We suggest that tests cover at least all configuration options and actions, as well as the observed Juju events, but this is not a requirement for listing.
* Some charms may have additional tests in an external location, particularly if the charm has specific resource requirements (such as specific hardware).

Please provide your review within *three working days*. If blocking issues are found, please help the author work through those, and respond to any follow-up posts within *one working day*.

## Listing requirements

* [ ] The charm does what it is meant to do, per the [demo or tutorial]({demo_url}).
* [ ] The [charm's page on Charmhub](https://charmhub.io/{name}) provides a quality impression. The overall appearance looks good and the [documentation]({documentation_link}) looks reasonable.
* [ ] The charm has an icon.
* [ ] [Automated releasing]({ci_release_url}) to unstable channels exists
* [ ] [Integration tests]({ci_integration_url}) exist, are run on every change to the default branch, and are passing. At minimum, the tests verify that the charm can be deployed and ends up in a success state, and that the charm can be integrated with at least one example for each 'provides' and 'requires' specified (including optional, excluding tracing) ending up in a success state. The tests should be run with `charmcraft test`.
""".strip()  # noqa: E501
    ]
    description.append(
        """
## Documentation

A charm's documentation should focus on the charm itself. For workload-specific or Juju-related content, link to the appropriate upstream documentation. A smaller charm can have single-page documentation for its description. A bigger charm should include a full Diátaxis navigation tree. Check that the charm has documentation that covers:
* [ ] How to use the charm, including configuration, limitations, and deviations in behaviour from the “non-charmed” version of the application.
* [ ] How to modify the charm
* [ ] A concise summary of the charm in the `charmcraft.yaml` 'summary' field, and a more detailed description in the `charmcraft.yaml` 'description' field.
""".strip(),  # noqa: E501
    )

    # fmt: on
    best_practices = find_best_practices(
        path_to_ops=path_to_ops, path_to_charmcraft=path_to_charmcraft
    )
    if best_practices:
        description.append('\n\n')
        description.append(
            f"""
## Best practices

The following best practices are recommended for all charms, and are also
required for listing.

{'\n'.join(('* [ ] ' + p) for p in best_practices)}
""".strip()
        )

    description.append(
        """
## Additional checks

The following checks are not required for listing, but are recommended for all charms.

* [ ] A user can deploy the charm with a sensible default configuration.
* [ ] The charm exposes provides / requires interfaces for integration ready to be adopted by the ecosystem.
* [ ] The upgrades the application safely, preserving data and settings, and minimising downtime.
* [ ] The charm supports scaling up and down, if the application permits or supports it.
* [ ] The charm supports backup and restore, if the application permits or supports it.
* [ ] The charm is integrated with observability, including metrics, alerting, and logging.
""".strip()  # noqa: E501
    )

    return ''.join(description)


IGNORED_BEST_PRACTICES = {
    # This is covered by the more extensive items above. It's also duplicated in
    # the docs.
    '* [ ] The quality assurance pipeline of a charm should be automated using a '
    'continuous integration (CI) system.',
    # This is duplicated by another best practice note.
    "* [ ] If you're setting up a ``git`` repository: name it using the pattern "
    '``<charm name>-operator``. For the charm name, see :ref:`specify-a-name`.',
    # These don't seem like best practice notes -- we should adjust the docs.
    '* [ ] Smaller charm documentation examples:',
    '* [ ] Bigger charm documentation examples:',
}


def extract_best_practice_blocks(file_path: pathlib.Path):
    """Extracts 'Best practice' blocks from a file."""
    remove_pattern: str | None = None
    matches: list[str] = []
    content = file_path.read_text()
    if file_path.suffix == '.md':
        matches = BEST_PRACTICE_RE_MD.findall(content)
        remove_pattern = r'^:class: hint\s*\n'
    elif file_path.suffix == '.rst':
        matches = BEST_PRACTICE_RE_REST.findall(content)
        remove_pattern = r'^\s+'
    assert remove_pattern is not None, 'Unsupported file type for best practices extraction.'
    if not matches:
        return matches
    return [
        re.sub(remove_pattern, '', match, flags=re.MULTILINE).strip().replace('\n', ' ')
        for match in matches
    ]


def find_best_practices(path_to_ops: pathlib.Path, path_to_charmcraft: pathlib.Path):
    """Recursively located best practice blocks in Ops and Charmcraft."""
    checklist: list[str] = []
    for directory in (path_to_ops, path_to_charmcraft):
        for file_path in directory.rglob('*'):
            if file_path.suffix in ('.md', '.rst'):
                practices = (
                    practice
                    for practice in extract_best_practice_blocks(file_path)
                    if practice not in IGNORED_BEST_PRACTICES
                )
                checklist.extend(practices)
    return checklist


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

    Requires `gh` CLI to be installed and authenticated, and `jq` to be
    available.
    """
    result = subprocess.run(  # noqa: S603
        ['gh', 'issue', 'view', str(issue_number), '--json', 'body', '--jq', '.body'],  # noqa: S607
        capture_output=True,
        text=True,
        check=True,
    )
    body = result.stdout

    # Define the fields to extract and their headings
    fields = {
        'name': '### Charm name',
        'demo_url': '### Demo',
        'project_repo': '### Project Repository',
        'ci_linting': '### CI Linting',
        'ci_release_url': '### CI Release',
        'ci_integration_url': '### CI Integration Tests',
        'documentation_link': '### Documentation Link',
    }

    # Extract values for each field
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


def assign_review(issue_number: int):
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
    reviewers_file = pathlib.Path(__file__).parent / 'reviewers.yaml'
    with reviewers_file.open('r') as f:
        reviewers_data = yaml.safe_load(f)
    reviewers = reviewers_data['reviewers']
    teams = [info['team'] for info in reviewers.values()]
    team = random.choice(teams)  # noqa: S311
    # If there happen to be multiple people in a team, then randomly pick among
    # them.
    team_reviewers = [name for name, info in reviewers.items() if info['team'] == team]
    reviewer = random.choice(team_reviewers)  # noqa: S311

    subprocess.run(  # noqa: S603
        ['gh', 'issue', 'edit', str(issue_number), '--assignee', reviewer],  # noqa: S607
        check=True,
    )


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
    parser.add_argument(
        '--path-to-ops',
        type=pathlib.Path,
        default=pathlib.Path(__file__).parent.parent / 'operator',
        help='Path to a clone of canonical/operator',
    )
    parser.add_argument(
        '--path-to-charmcraft',
        type=pathlib.Path,
        default=pathlib.Path(__file__).parent.parent / 'charmcraft',
        help='Path to a clone of canonical/charmcraft',
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
        args.path_to_ops,
        args.path_to_charmcraft,
    )
    results = evaluate.evaluate(
        issue_data['name'],
        issue_data['project_repo'],
        issue_data['ci_linting'],
        issue_data['contribution_link'],
        issue_data['license_link'],
        issue_data['security_link'],
    )
    for result in results:
        if result.replace('* [ ]', '* [x]') in comment:
            # TODO: Should we support unticking? The reviewer needs to be able to override
            # the checklist. However, the publisher should not be able to override and one way
            # to enforce that is to have the automatic check 'win' (we would also need to
            # trigger if the comment changes and not loop).
            comment = comment.replace(result, result.replace('* [ ]', '* [x]'))
        elif result.replace('* [x]', '* [ ]') in comment:
            comment = comment.replace(result.replace('* [x]', '* [ ]'), result)

    if args.dry_run:
        print(summary)
        print()
        print(comment)
    else:
        # Update the issue title:
        subprocess.run(  # noqa: S603
            ['gh', 'issue', 'edit', str(args.issue_number), '--title', summary],  # noqa: S607
            check=True,
        )
        # If there is already a first comment that we posted, then update it with the latest
        # content. Otherwise, create a new comment.
        existing_comments = subprocess.run(  # noqa: S603
            ['gh', 'issue', 'view', str(args.issue_number), '--json', 'comments'],  # noqa: S607
            capture_output=True,
            text=True,
            check=True,
        )
        existing_comments = json.loads(existing_comments.stdout.strip()).get('comments', [])
        if existing_comments:
            # Update the first comment with the new content.
            subprocess.run(  # noqa: S603
                [  # noqa: S607
                    'gh',
                    'issue',
                    'comment',
                    str(args.issue_number),
                    '--edit-last',
                    existing_comments.splitlines()[0],
                    '--body',
                    comment,
                ],
                check=True,
            )
        else:
            # Create a new comment.
            subprocess.run(  # noqa: S603
                ['gh', 'issue', 'comment', str(args.issue_number), '--body', comment],  # noqa: S607
                check=True,
            )


if __name__ == '__main__':
    main()
