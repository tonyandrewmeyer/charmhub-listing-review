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

"""Test the issue comment generation."""

import json
import pathlib
from unittest import mock

import charmhub_listing_review.update_issue as update_issue


@mock.patch('random.choice')
@mock.patch('subprocess.run')
@mock.patch('yaml.safe_load')
@mock.patch('pathlib.Path.open')
def test_assign_review_multiple_teams(
    mock_open, mock_yaml_load, mock_subprocess_run, mock_random_choice
):
    reviewers_yaml = {
        'reviewers': {
            '@alice': {'team': 'team1'},
            '@bob': {'team': 'team2'},
            '@carol': {'team': 'team1'},
        }
    }
    mock_yaml_load.return_value = reviewers_yaml
    mock_open.return_value.__enter__.return_value = mock.Mock()
    mock_subprocess_run.return_value = mock.Mock()
    mock_random_choice.return_value = '@bob'
    reviewer = update_issue.assign_review(42, pathlib.Path('reviewers.yaml'))
    assert reviewer == '@bob'
    mock_subprocess_run.assert_called_once_with(
        [
            'gh',
            'issue',
            'edit',
            '42',
            '--add-assignee',
            'bob',
        ],
        check=True,
    )


@mock.patch('subprocess.run')
@mock.patch('yaml.safe_load')
@mock.patch('pathlib.Path.open')
def test_assign_review_single_team(mock_open, mock_yaml_load, mock_subprocess_run):
    reviewers_yaml = {
        'reviewers': {
            '@alice': {'team': 'team1'},
        }
    }
    mock_yaml_load.return_value = reviewers_yaml
    mock_open.return_value.__enter__.return_value = mock.Mock()
    mock_subprocess_run.return_value = mock.Mock()
    reviewer = update_issue.assign_review(99, pathlib.Path('reviewers.yaml'))
    assert reviewer == '@alice'
    mock_subprocess_run.assert_called_once_with(
        [
            'gh',
            'issue',
            'edit',
            '99',
            '--add-assignee',
            'alice',
        ],
        check=True,
    )


@mock.patch('subprocess.run')
def test_assign_to_overrides_automatic_assignment(mock_subprocess_run):
    empty_comments = json.dumps({'comments': []})
    mock_subprocess_run.return_value = mock.Mock(returncode=0, stdout=empty_comments)
    update_issue.update_gh_issue(
        issue_number=42,
        summary='Review `my-charm` for public listing on Charmhub',
        comment='test comment',
        reviewers_file=pathlib.Path('reviewers.yaml'),
        assign_to='tonyandrewmeyer',
    )
    # Should assign to the specified user, not pick randomly.
    mock_subprocess_run.assert_any_call(
        ['gh', 'issue', 'edit', '42', '--add-assignee', 'tonyandrewmeyer'],
        check=True,
    )


@mock.patch('subprocess.run')
def test_assign_to_strips_at_prefix(mock_subprocess_run):
    empty_comments = json.dumps({'comments': []})
    mock_subprocess_run.return_value = mock.Mock(returncode=0, stdout=empty_comments)
    update_issue.update_gh_issue(
        issue_number=42,
        summary='Review `my-charm` for public listing on Charmhub',
        comment='test comment',
        reviewers_file=pathlib.Path('reviewers.yaml'),
        assign_to='@tonyandrewmeyer',
    )
    mock_subprocess_run.assert_any_call(
        ['gh', 'issue', 'edit', '42', '--add-assignee', 'tonyandrewmeyer'],
        check=True,
    )


@mock.patch('subprocess.run')
def test_assign_to_dry_run_does_not_call_gh(mock_subprocess_run):
    empty_comments = json.dumps({'comments': []})
    mock_subprocess_run.return_value = mock.Mock(returncode=0, stdout=empty_comments)
    update_issue.update_gh_issue(
        issue_number=42,
        summary='Review `my-charm` for public listing on Charmhub',
        comment='test comment',
        reviewers_file=pathlib.Path('reviewers.yaml'),
        dry_run=True,
        assign_to='tonyandrewmeyer',
    )
    # In dry-run with assign_to, no gh issue edit calls should be made.
    for call in mock_subprocess_run.call_args_list:
        args = call[0][0] if call[0] else call[1].get('args', [])
        assert '--add-assignee' not in args


@mock.patch('subprocess.run')
def test_get_details_from_issue(mock_subprocess_run):
    issue_body = """
### Charm name
my-charm

### Demo
https://demo.example.com

### Project Repository
https://github.com/canonical/my-charm

### CI Linting
https://ci.example.com/lint

### CI Release
https://ci.example.com/release

### CI Integration Tests
https://ci.example.com/integration

### Documentation Link
https://docs.example.com
"""
    mock_subprocess_run.return_value = mock.Mock(stdout=json.dumps({'body': issue_body}))
    details = update_issue.get_details_from_issue(123)
    assert details['name'] == 'my-charm'
    assert details['demo_url'] == 'https://demo.example.com'
    assert details['project_repo'] == 'https://github.com/canonical/my-charm'
    assert details['ci_linting'] == 'https://ci.example.com/lint'
    assert details['ci_release_url'] == 'https://ci.example.com/release'
    assert details['ci_integration_url'] == 'https://ci.example.com/integration'
    assert details['documentation_link'] == 'https://docs.example.com'
    assert (
        details['contribution_link']
        == 'https://github.com/canonical/my-charm/blob/main/CONTRIBUTING.md'
    )
    assert details['license_link'] == 'https://github.com/canonical/my-charm/blob/main/LICENSE'
    assert (
        details['security_link'] == 'https://github.com/canonical/my-charm/blob/main/SECURITY.md'
    )


def test_issue_summary():
    name = 'my-charm'
    summary = update_issue.issue_summary(name)
    assert summary == 'Review `my-charm` for public listing on Charmhub'


def _make_issue_data(**overrides):
    defaults = {
        'name': 'my-charm',
        'demo_url': 'https://demo.example.com',
        'project_repo': 'https://github.com/canonical/my-charm',
        'ci_linting': 'https://ci.example.com/lint',
        'ci_release_url': 'https://ci.example.com/release',
        'ci_integration_url': 'https://ci.example.com/integration',
        'documentation_link': 'https://docs.example.com',
        'contribution_link': 'https://github.com/canonical/my-charm/blob/main/CONTRIBUTING.md',
        'license_link': 'https://github.com/canonical/my-charm/blob/main/LICENSE',
        'security_link': 'https://github.com/canonical/my-charm/blob/main/SECURITY.md',
    }
    defaults.update(overrides)
    return defaults


def test_apply_automated_checks_ticks_passed():
    """apply_automated_checks replaces unchecked items with checked for passing results."""
    from charmhub_listing_review.evaluate import CheckResult

    result = CheckResult(
        name='license',
        passed=True,
        description='* [x] The charm provides a license statement.',
        context={},
    )
    comment = '* [ ] The charm provides a license statement.'
    with (
        mock.patch('charmhub_listing_review.update_issue.evaluate', return_value=[result]),
        mock.patch('charmhub_listing_review.update_issue.is_ai_available', return_value=False),
    ):
        output = update_issue.apply_automated_checks(_make_issue_data(), comment)
    assert '* [x] The charm provides a license statement.' in output


def test_apply_automated_checks_ai_explanation():
    """apply_automated_checks adds AI explanation sub-bullets for failed checks."""
    from charmhub_listing_review.evaluate import CheckResult

    result = CheckResult(
        name='license',
        passed=False,
        description='* [x] The charm provides a license statement.',
        context={},
        ai_explanation='The LICENSE file was not recognised.',
    )
    comment = '* [ ] The charm provides a license statement.'
    with (
        mock.patch('charmhub_listing_review.update_issue.evaluate', return_value=[result]),
        mock.patch('charmhub_listing_review.update_issue.is_ai_available', return_value=False),
    ):
        output = update_issue.apply_automated_checks(_make_issue_data(), comment)
    assert '_AI: The LICENSE file was not recognised._' in output


def test_apply_automated_checks_ai_disabled():
    """apply_automated_checks does not call explain_failures when AI is unavailable."""
    from charmhub_listing_review.evaluate import CheckResult

    result = CheckResult(
        name='license',
        passed=False,
        description='* [x] The charm provides a license statement.',
        context={},
    )
    comment = '* [ ] The charm provides a license statement.'
    with (
        mock.patch('charmhub_listing_review.update_issue.evaluate', return_value=[result]),
        mock.patch('charmhub_listing_review.update_issue.is_ai_available', return_value=False),
        mock.patch('charmhub_listing_review.update_issue.explain_failures') as mock_explain,
    ):
        update_issue.apply_automated_checks(_make_issue_data(), comment)
    mock_explain.assert_not_called()


def test_apply_automated_checks_ai_error_is_graceful():
    """apply_automated_checks still succeeds when explain_failures raises."""
    from charmhub_listing_review.evaluate import CheckResult

    result = CheckResult(
        name='license',
        passed=False,
        description='* [x] The charm provides a license statement.',
        context={},
    )
    comment = '* [ ] The charm provides a license statement.'
    with (
        mock.patch('charmhub_listing_review.update_issue.evaluate', return_value=[result]),
        mock.patch('charmhub_listing_review.update_issue.is_ai_available', return_value=True),
        mock.patch(
            'charmhub_listing_review.update_issue.explain_failures',
            side_effect=RuntimeError('Copilot auth failed'),
        ),
    ):
        # Should not raise — AI errors are swallowed.
        output = update_issue.apply_automated_checks(_make_issue_data(), comment)
    assert '* [x] The charm provides a license statement.' in output
