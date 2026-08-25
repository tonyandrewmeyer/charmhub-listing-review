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

"""Test the self-review console output."""

from unittest import mock

import charmhub_listing_review.self_review as self_review
from charmhub_listing_review.evaluate import CheckResult, EvaluationResult


@mock.patch('charmhub_listing_review.self_review.get_default_branch', return_value='main')
@mock.patch('charmhub_listing_review.self_review.evaluate')
def test_optional_check_failure_is_manual_review_not_failed(mock_evaluate, _mock_branch, capsys):
    """A failed optional check (e.g. the icon) stays '❓', not '❌'."""
    mock_evaluate.return_value = EvaluationResult(
        checks=[
            CheckResult(
                name='charm_has_icon',
                passed=False,
                description='* [ ] The charm has an icon (recommended).',
                checklist_id='charm-has-icon',
                optional=True,
            ),
        ]
    )
    self_review.print_self_review_results(
        'my-charm', project_repo='https://github.com/org/my-charm'
    )
    output = capsys.readouterr().out
    assert '❓' in output
    icon_lines = [line for line in output.splitlines() if 'icon' in line]
    assert all('❌' not in line for line in icon_lines)
    assert any('❓' in line for line in icon_lines)


@mock.patch('charmhub_listing_review.self_review.get_default_branch', return_value='main')
@mock.patch('charmhub_listing_review.self_review.evaluate')
def test_required_check_failure_is_marked_failed(mock_evaluate, _mock_branch, capsys):
    """A failed non-optional check with a matching ID is marked '❌'."""
    mock_evaluate.return_value = EvaluationResult(
        checks=[
            CheckResult(
                name='security_doc',
                passed=False,
                description='* [ ] The charm provides a security statement.',
                checklist_id='doc-security-statement',
            ),
        ]
    )
    self_review.print_self_review_results(
        'my-charm', project_repo='https://github.com/org/my-charm'
    )
    output = capsys.readouterr().out
    security_lines = [line for line in output.splitlines() if 'security statement' in line]
    assert any('❌' in line for line in security_lines)
