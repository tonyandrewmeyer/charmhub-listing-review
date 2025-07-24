#! /usr/bin/env python3

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

import os
import pathlib
import re
import subprocess  # noqa: S404

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


def issue_description(
    name: str,
    demo_url: str,
    ci_release_url: str,
    ci_integration_url: str,
    documentation_link: str,
    has_library: bool,
    path_to_ops: pathlib.Path,
    path_to_charmcraft: pathlib.Path,
):
    """Provide a suitable issue description."""
    # TODO: Fix the text wrapping - it needs to be soft, not hard.
    # TODO: The links to CONTRIBUTING.md and README.md don't work.
    # TODO: Update the documentation expectations based on the discussion with David.
    description = [
        f"""
Please review the charm by checking each of the items in the following checklist.

If you find other improvements or fixes that could be made to the charm, feel
free to suggest those, but **they do not block listing**. If you find something
that's missing from the review checklist or best practices, please separately
suggest that (see [CONTRIBUTING.md](./CONTRIBUTING.md)) so that we can keep the
review process consistent.

See the [README](./README.md) for more information about the charm listing
review process.

When reviewing test coverage of the charm, note that:

* Unit tests are recommended, but *not* required.
* A minimal set of integration tests is required, as outlined in the checklist.
* There is no minimum for test coverage. We suggest that tests cover at least
  all configuration options and actions, as well as the observed Juju events,
  but this is not a requirement for listing.
* Some charms may have additional tests in an external location, particularly if
  the charm has specific resource requirements (such as specific hardware).

Please provide your review within *three working days*. If blocking issues are
found, please help the author work through those, and respond to any follow-up
posts within *one working day*.

## Listing requirements

* [ ] The charm does what it is meant to do, per the [demo or tutorial]({demo_url}).
* [ ] The [charm's page on Charmhub](https://charmhub.io/{name}) provides a
  quality impression. The overall appearance looks good and the
  [documentation]({documentation_link}) looks reasonable.
* [ ] [Automated releasing]({ci_release_url}) to unstable channels exists
* [ ] [Integration tests]({ci_integration_url}) exist, are run on every change
  to the default branch, and are passing. At minimum, the tests verify that the
  charm can be deployed and ends up in a success state, and that the charm can
  be integrated with at least one example for each 'provides' and 'requires'
  specified (including optional, excluding tracing) ending up in a success
  state. The tests should be run with `charmcraft test`.
""".strip()
    ]
    if has_library:
        description.append('\n\n')
        description.append(
            """
If the charm provides an interface library, the library's module docstring must
contain the following information:
* [ ] the interface(s) this library is for, and if it takes care of one or both
  of the providing/requiring sides
* [ ] guidance on how to start when using the library to implement their end of
  the interface

If the charm provides a general library, the library's module docstring must
contain the following information:
* [ ] the purpose of the library
* [ ] the intended audience for the library: is this library intended for use
  only by the charm or the charming team, or is it a public library intended for
  anyone to use in their charm?
* [ ] guidance on how to start using the library
""".strip()
        )
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
    content = file_path.read_text()
    if file_path.suffix == '.md':
        matches = BEST_PRACTICE_RE_MD.findall(content)
        remove_pattern = r'^:class: hint\s*\n'
    elif file_path.suffix == '.rst':
        matches = BEST_PRACTICE_RE_REST.findall(content)
        remove_pattern = r'^\s+'
    return [
        re.sub(remove_pattern, '', match, flags=re.MULTILINE).strip().replace('\n', ' ')
        for match in matches
    ]


def find_best_practices(path_to_ops: pathlib.Path, path_to_charmcraft: pathlib.Path):
    """Recursively located best practice blocks in Ops and Charmcraft."""
    checklist = []
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


def get_details_from_issue(issue_number: int) -> dict[str, str]:
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
        'has_library': '### Charm Libraries',
        'project_repo': '### Project Repository',
        'ci_linting': '### CI Linting',
        'ci_release_url': '### CI Release',
        'ci_integration_url': '### CI Integration Tests',
        'documentation_link': '### Documentation Link',
        'contribution_link': '### Contribution Link',
        'license_link': '### License Link',
        'security_link': '### Security Link',
    }

    # Extract values for each field
    issue_data: dict[str, str] = {}
    for key, heading in fields.items():
        pattern = rf'{re.escape(heading)}\s*\n([^\n]*)'
        match = re.search(pattern, body)
        if match:
            value = match.group(1).strip()
            # this breaks the pretty types, fix it later.
            if key == 'has_library':
                issue_data[key] = value.lower() in ('yes', 'true', '1')
            else:
                issue_data[key] = value
        else:
            issue_data[key] = None

    return issue_data


if __name__ == '__main__':
    import argparse

    import evaluate

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

    if args.dry_run:
        print(issue_summary(issue_data['name']))
        print()
        description = issue_description(
            issue_data['name'],
            issue_data['demo_url'],
            issue_data['ci_release_url'],
            issue_data['ci_integration_url'],
            issue_data['documentation_link'],
            issue_data['has_library'],
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
            os.environ.get("SUBDIR"),
        )
        for result in results:
            if result.replace('* [ ]', '* [x]') in description:
                # TODO: Should we support unticking? The reviewer needs to be able to override
                # the checklist. However, the publisher should not be able to override and one way
                # to enforce that is to have the automatic check 'win' (we would also need to
                # trigger if the description changes and not loop).
                description = description.replace(result, result.replace('* [ ]', '* [x]'))
            elif result.replace('* [x]', '* [ ]') in description:
                description = description.replace(result.replace('* [x]', '* [ ]'), result)
        print(description)
