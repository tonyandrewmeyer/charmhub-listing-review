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
from charmhub_listing_review.ai_client import assess_documentation, assess_metadata
from charmhub_listing_review.ai_code_review import analyse_code, collect_charm_code
from charmhub_listing_review.evaluate import CheckResult, EvaluationResult, _gather_doc_context
from charmhub_listing_review.interactive import _build_context_prompt, run_interactive
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


def test_generate_summary():
    results = [
        _make_result('check_a', passed=True, description='* [x] Check A passed.'),
        _make_result('check_b', passed=False, description='* [ ] Check B failed.'),
        _make_result('check_c', passed=None, description='* [ ] Check C needs review.'),
    ]

    with (
        mock.patch('charmhub_listing_review.ai_client.start_client', new_callable=mock.AsyncMock),
        mock.patch('charmhub_listing_review.ai_client.stop_client', new_callable=mock.AsyncMock),
        mock.patch(
            'charmhub_listing_review.ai_client.create_session',
            new_callable=mock.AsyncMock,
        ),
        mock.patch(
            'charmhub_listing_review.ai_client.send_prompt',
            new_callable=mock.AsyncMock,
            return_value='- PRIORITY: Fix Check B.\n- GOOD: Check A passes.',
        ),
    ):
        summary = asyncio.run(ai_client.generate_summary('test-charm', results))

    assert 'PRIORITY' in summary
    assert 'Check B' in summary


def test_assess_documentation_sanitises_output():
    """assess_documentation strips dangerous content from LLM output."""
    malicious_response = (
        'Looks good! Visit <script>alert(1)</script> or '
        '[click here](https://evil.example.com) for more.'
    )
    doc_context = {'readme_content': '# Charm'}

    with (
        mock.patch('charmhub_listing_review.ai_client.start_client', new_callable=mock.AsyncMock),
        mock.patch('charmhub_listing_review.ai_client.stop_client', new_callable=mock.AsyncMock),
        mock.patch(
            'charmhub_listing_review.ai_client.create_session',
            new_callable=mock.AsyncMock,
        ),
        mock.patch(
            'charmhub_listing_review.ai_client.send_prompt',
            new_callable=mock.AsyncMock,
            return_value=malicious_response,
        ),
    ):
        result = asyncio.run(assess_documentation(doc_context))

    assert '<script>' not in result
    assert 'https://evil.example.com' not in result
    assert 'click here' in result  # Link text is preserved, URL stripped.


def test_assess_metadata_sanitises_output():
    """assess_metadata strips dangerous content from LLM output."""
    malicious_response = '- Title: ![tracker](https://evil.example.com/pixel.png) OK'
    charmcraft_data = {'name': 'my-charm', 'title': 'My Charm'}

    with (
        mock.patch('charmhub_listing_review.ai_client.start_client', new_callable=mock.AsyncMock),
        mock.patch('charmhub_listing_review.ai_client.stop_client', new_callable=mock.AsyncMock),
        mock.patch(
            'charmhub_listing_review.ai_client.create_session',
            new_callable=mock.AsyncMock,
        ),
        mock.patch(
            'charmhub_listing_review.ai_client.send_prompt',
            new_callable=mock.AsyncMock,
            return_value=malicious_response,
        ),
    ):
        result = asyncio.run(assess_metadata(charmcraft_data))

    assert 'https://evil.example.com' not in result
    assert '![' not in result


def test_assess_documentation():
    doc_context = {
        'readme_content': '# My Charm\nA charm for things.',
        'doc_files': ['docs/tutorial.md', 'docs/reference.md'],
        'documentation_url': 'https://docs.example.com',
    }

    with (
        mock.patch('charmhub_listing_review.ai_client.start_client', new_callable=mock.AsyncMock),
        mock.patch('charmhub_listing_review.ai_client.stop_client', new_callable=mock.AsyncMock),
        mock.patch(
            'charmhub_listing_review.ai_client.create_session',
            new_callable=mock.AsyncMock,
        ),
        mock.patch(
            'charmhub_listing_review.ai_client.send_prompt',
            new_callable=mock.AsyncMock,
            return_value='Needs work: missing usage examples.',
        ) as mock_send,
    ):
        result = asyncio.run(assess_documentation(doc_context))

    assert 'usage examples' in result
    prompt_arg = mock_send.call_args[0][1]
    assert 'My Charm' in prompt_arg
    assert 'docs/tutorial.md' in prompt_arg


def test_assess_metadata():
    charmcraft_data = {
        'name': 'my-charm',
        'title': 'My Charm',
        'summary': 'A test charm.',
        'description': 'This charm does things.',
    }

    with (
        mock.patch('charmhub_listing_review.ai_client.start_client', new_callable=mock.AsyncMock),
        mock.patch('charmhub_listing_review.ai_client.stop_client', new_callable=mock.AsyncMock),
        mock.patch(
            'charmhub_listing_review.ai_client.create_session',
            new_callable=mock.AsyncMock,
        ),
        mock.patch(
            'charmhub_listing_review.ai_client.send_prompt',
            new_callable=mock.AsyncMock,
            return_value='- Summary: Good.\n- Description: Needs more detail.',
        ) as mock_send,
    ):
        result = asyncio.run(assess_metadata(charmcraft_data))

    assert 'Description' in result
    prompt_arg = mock_send.call_args[0][1]
    assert 'My Charm' in prompt_arg


def test_assess_metadata_empty():
    result = asyncio.run(assess_metadata({}))
    assert result == ''


def test_gather_doc_context(tmp_path):
    readme = tmp_path / 'README.md'
    readme.write_text('# My Charm\nSome docs here.')
    docs_dir = tmp_path / 'docs'
    docs_dir.mkdir()
    (docs_dir / 'tutorial.md').write_text('# Tutorial')

    charmcraft = {'links': {'documentation': 'https://docs.example.com'}}
    ctx = _gather_doc_context(tmp_path, charmcraft)

    assert '# My Charm' in ctx['readme_content']
    assert 'docs/tutorial.md' in ctx['doc_files']
    assert ctx['documentation_url'] == 'https://docs.example.com'


def test_gather_doc_context_no_docs(tmp_path):
    ctx = _gather_doc_context(tmp_path, None)
    assert 'readme_content' not in ctx
    assert 'doc_files' not in ctx


def test_collect_charm_code(tmp_path):
    src_dir = tmp_path / 'src'
    src_dir.mkdir()
    (src_dir / 'charm.py').write_text('class MyCharm: pass')
    (src_dir / 'helpers.py').write_text('def helper(): pass')

    code = collect_charm_code(tmp_path)
    assert 'src/charm.py' in code
    assert 'src/helpers.py' in code
    assert 'class MyCharm' in code['src/charm.py']


def test_collect_charm_code_empty(tmp_path):
    code = collect_charm_code(tmp_path)
    assert code == {}


def test_analyse_code():
    code_context = {'src/charm.py': 'class MyCharm: pass'}

    with (
        mock.patch(
            'charmhub_listing_review.ai_code_review.start_client', new_callable=mock.AsyncMock
        ),
        mock.patch(
            'charmhub_listing_review.ai_code_review.stop_client', new_callable=mock.AsyncMock
        ),
        mock.patch(
            'charmhub_listing_review.ai_code_review.create_session',
            new_callable=mock.AsyncMock,
        ),
        mock.patch(
            'charmhub_listing_review.ai_code_review.send_prompt',
            new_callable=mock.AsyncMock,
            return_value='- warning: Missing status updates.',
        ),
    ):
        result = asyncio.run(analyse_code(code_context))

    assert 'Missing status' in result


def test_analyse_code_empty():
    result = asyncio.run(analyse_code({}))
    assert result == ''


def test_generate_summary_with_metadata():
    results = [_make_result('check_a', passed=True, description='* [x] Check A.')]
    metadata = {'name': 'my-charm', 'title': 'My Charm', 'summary': 'A test charm.'}

    with (
        mock.patch('charmhub_listing_review.ai_client.start_client', new_callable=mock.AsyncMock),
        mock.patch('charmhub_listing_review.ai_client.stop_client', new_callable=mock.AsyncMock),
        mock.patch(
            'charmhub_listing_review.ai_client.create_session',
            new_callable=mock.AsyncMock,
        ),
        mock.patch(
            'charmhub_listing_review.ai_client.send_prompt',
            new_callable=mock.AsyncMock,
            return_value='All checks pass.',
        ) as mock_send,
    ):
        summary = asyncio.run(
            ai_client.generate_summary('my-charm', results, charmcraft_data=metadata)
        )

    assert summary == 'All checks pass.'
    # Verify metadata was included in the prompt.
    prompt_arg = mock_send.call_args[0][1]
    assert 'My Charm' in prompt_arg


def test_build_context_prompt():
    evaluation = EvaluationResult(
        checks=[
            _make_result('check_a', passed=True, description='* [x] Check A passed.'),
            _make_result('check_b', passed=False, description='* [ ] Check B failed.'),
        ],
        charmcraft_data={'name': 'my-charm', 'title': 'My Charm', 'summary': 'A test.'},
        doc_context={'readme_content': '# My Charm\nDocs here.'},
        code_context={'code_files': {'src/charm.py': 'class MyCharm: pass'}},
    )

    prompt = _build_context_prompt('my-charm', evaluation)
    assert 'my-charm' in prompt
    assert '1 passed' in prompt
    assert '1 failed' in prompt
    assert 'PASSED' in prompt
    assert 'FAILED' in prompt
    assert 'My Charm' in prompt
    assert '# My Charm' in prompt
    assert 'class MyCharm' in prompt


def test_run_interactive_ai_unavailable(capsys):
    evaluation = EvaluationResult(checks=[])

    with mock.patch('charmhub_listing_review.interactive.is_ai_available', return_value=False):
        run_interactive('my-charm', evaluation)

    output = capsys.readouterr().out
    assert 'Copilot SDK' in output
