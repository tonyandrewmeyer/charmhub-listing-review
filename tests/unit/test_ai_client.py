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

"""Test the AI client module."""

import asyncio
from unittest import mock

import charmhub_listing_review.ai_client as ai_client
from charmhub_listing_review.evaluate import CheckResult
from charmhub_listing_review.self_review import format_checklist_for_console


def _make_result(name, passed, description='* [ ] Test check.', **ctx):
    return CheckResult(name=name, passed=passed, description=description, context=ctx)


@mock.patch('charmhub_listing_review.ai_client.shutil.which', return_value=None)
def test_ai_not_available_no_cli(mock_which):
    """AI is unavailable when the Copilot CLI is not found."""
    ai_client._copilot_available = None  # Reset cache.
    assert ai_client.is_ai_available() is False
    ai_client._copilot_available = None  # Clean up.


@mock.patch('charmhub_listing_review.ai_client.shutil.which', return_value='/usr/bin/copilot')
def test_ai_not_available_no_sdk(mock_which):
    """AI is unavailable when the SDK cannot be imported."""
    ai_client._copilot_available = None  # Reset cache.
    with mock.patch.dict('sys.modules', {'copilot': None}):
        # Importing 'copilot' will raise ImportError when set to None.
        assert ai_client.is_ai_available() is False
    ai_client._copilot_available = None  # Clean up.


def test_explain_failures_populates_explanations():
    """explain_failures sends prompts for failed checks and populates ai_explanation."""
    results = [
        _make_result('check_a', passed=True),
        _make_result('check_b', passed=False, url='https://example.com'),
        _make_result('check_c', passed=None),
        _make_result('check_d', passed=False, error='not found'),
    ]

    mock_session = mock.AsyncMock()
    mock_response = mock.Mock()
    mock_response.data.content = 'Fix by doing X.'
    mock_session.send_and_wait.return_value = mock_response

    with (
        mock.patch('charmhub_listing_review.ai_client.start_client', new_callable=mock.AsyncMock),
        mock.patch('charmhub_listing_review.ai_client.stop_client', new_callable=mock.AsyncMock),
        mock.patch(
            'charmhub_listing_review.ai_client.create_session',
            new_callable=mock.AsyncMock,
            return_value=mock_session,
        ),
        mock.patch(
            'charmhub_listing_review.ai_client.send_prompt',
            new_callable=mock.AsyncMock,
            return_value='Fix by doing X.',
        ),
    ):
        updated = asyncio.run(ai_client.explain_failures(results))

    # Only failed checks get explanations.
    assert updated[0].ai_explanation == ''  # passed=True
    assert updated[1].ai_explanation == 'Fix by doing X.'  # passed=False
    assert updated[2].ai_explanation == ''  # passed=None
    assert updated[3].ai_explanation == 'Fix by doing X.'  # passed=False


def test_explain_failures_no_failures_skips_ai():
    """explain_failures returns results unchanged when nothing failed."""
    results = [
        _make_result('check_a', passed=True),
        _make_result('check_b', passed=None),
    ]

    # Should not call any AI functions.
    with mock.patch(
        'charmhub_listing_review.ai_client.start_client', new_callable=mock.AsyncMock
    ) as mock_start:
        updated = asyncio.run(ai_client.explain_failures(results))

    mock_start.assert_not_called()
    assert updated is results


def test_format_checklist_with_ai_explanations():
    checklist = '* [o] The charm provides a license statement.'
    explanations = {
        '* [ ] The charm provides a license statement.': 'Your LICENSE file was not recognised.',
    }
    output = format_checklist_for_console(checklist, ai_explanations=explanations)
    assert 'Your LICENSE file was not recognised.' in output
    assert '\u274c' in output


def test_format_checklist_without_ai_explanations():
    checklist = '* [o] The charm provides a license statement.'
    output = format_checklist_for_console(checklist)
    assert '\u274c' in output
    # No AI text should appear.
    assert 'AI' not in output


def test_sanitise_ai_output():
    """_sanitise_ai_output collapses lines and escapes markdown."""
    result = ai_client._sanitise_ai_output('Line one.\nLine two with *bold* and _italic_.')
    assert '\n' not in result
    assert r'\*' in result
    assert r'\_' in result
    assert 'Line one. Line two' in result
