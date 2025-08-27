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
    reviewer = update_issue.assign_review(42)
    assert reviewer == '@bob'
    mock_subprocess_run.assert_called_once_with([
        'gh',
        'issue',
        'edit',
        '42',
        '--add-assignee',
        'bob',
    ], check=True)


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
    reviewer = update_issue.assign_review(99)
    assert reviewer == '@alice'
    mock_subprocess_run.assert_called_once_with([
        'gh',
        'issue',
        'edit',
        '99',
        '--add-assignee',
        'alice',
    ], check=True)


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
    mock_subprocess_run.return_value = mock.Mock(stdout=issue_body)
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


def test_extract_best_practice_blocks_markdown(tmp_path):
    md_file = tmp_path / 'test.md'
    md_file.write_text(
        """
Some intro text

```{admonition} Best practice
:class: hint

This is a markdown best practice block.
```

Other text

```{admonition} Best practice
:class: hint

Another best practice block.
```

Here is some more text.
```{tip}

This is just a tip, not a best practice.
```

Summary text.
"""
    )
    blocks = update_issue.extract_best_practice_blocks(md_file)
    assert 'This is a markdown best practice block.' in blocks[0]
    assert 'Another best practice block.' in blocks[1]


def test_extract_best_practice_blocks_rest(tmp_path):
    rst_file = tmp_path / 'test.rst'
    rst_file.write_text(
        """
Some intro text

.. admonition:: Best practice
    :class: hint

    This is a ReST best practice block.

Other text

.. admonition:: Best practice
    :class: hint

    Another best practice block.
"""
    )
    blocks = update_issue.extract_best_practice_blocks(rst_file)
    assert 'This is a ReST best practice block.' in blocks[0]
    assert 'Another best practice block.' in blocks[1]


def test_find_best_practices(tmp_path):
    ops_dir = tmp_path / 'ops'
    charmcraft_dir = tmp_path / 'charmcraft'
    ops_dir.mkdir()
    charmcraft_dir.mkdir()
    (ops_dir / 'file1.md').write_text(
        """
```{admonition} Best practice
:class: hint

Ops best practice.
```
"""
    )
    (charmcraft_dir / 'file2.rst').write_text(
        """
.. admonition:: Best practice
    :class: hint

    Charmcraft best practice.
"""
    )
    practices = update_issue.find_best_practices(ops_dir, charmcraft_dir)
    assert any('Ops best practice.' in p for p in practices)
    assert any('Charmcraft best practice.' in p for p in practices)


def test_issue_summary():
    name = 'my-charm'
    summary = update_issue.issue_summary(name)
    assert summary == 'Review `my-charm` for public listing on Charmhub'
