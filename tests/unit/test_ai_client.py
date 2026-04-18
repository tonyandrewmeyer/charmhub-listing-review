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

"""Test the AI client module and backend abstraction."""

import asyncio
from unittest import mock

import charmhub_listing_review.ai_client as ai_client
from charmhub_listing_review.ai_client import assess_documentation, assess_metadata
from charmhub_listing_review.evaluate import CheckResult, _gather_doc_context
from charmhub_listing_review.self_review import format_checklist_for_console


def _make_result(name, passed, description='* [ ] Test check.', **ctx):
    return CheckResult(name=name, passed=passed, description=description, context=ctx)


def _make_mock_backend(response='Fix by doing X.'):
    """Create a mock AIBackend with a configurable response."""
    backend = mock.AsyncMock()
    backend.is_available.return_value = True
    session = mock.AsyncMock()
    session.send.return_value = response
    backend.create_session.return_value = session
    backend.send_message.return_value = response
    return backend


def test_explain_failures_populates_explanations():
    """explain_failures sends prompts for failed checks and populates ai_explanation."""
    results = [
        _make_result('check_a', passed=True),
        _make_result('check_b', passed=False, url='https://example.com'),
        _make_result('check_c', passed=None),
        _make_result('check_d', passed=False, error='not found'),
    ]

    backend = _make_mock_backend()
    updated = asyncio.run(ai_client.explain_failures(backend, results))

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

    backend = _make_mock_backend()
    updated = asyncio.run(ai_client.explain_failures(backend, results))

    backend.start.assert_not_called()
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

    backend = _make_mock_backend('- PRIORITY: Fix Check B.\n- GOOD: Check A passes.')
    summary = asyncio.run(ai_client.generate_summary(backend, 'test-charm', results))

    assert 'PRIORITY' in summary
    assert 'Check B' in summary


def test_assess_documentation_sanitises_output():
    """assess_documentation strips dangerous content from LLM output."""
    malicious_response = (
        'Looks good! Visit <script>alert(1)</script> or '
        '[click here](https://evil.example.com) for more.'
    )
    doc_context = {'readme_content': '# Charm'}

    backend = _make_mock_backend(malicious_response)
    result = asyncio.run(assess_documentation(backend, doc_context))

    assert '<script>' not in result
    assert 'https://evil.example.com' not in result
    assert 'click here' in result  # Link text is preserved, URL stripped.


def test_assess_metadata_sanitises_output():
    """assess_metadata strips dangerous content from LLM output."""
    malicious_response = '- Title: ![tracker](https://evil.example.com/pixel.png) OK'
    charmcraft_data = {'name': 'my-charm', 'title': 'My Charm'}

    backend = _make_mock_backend(malicious_response)
    result = asyncio.run(assess_metadata(backend, charmcraft_data))

    assert 'https://evil.example.com' not in result
    assert '![' not in result


def test_assess_documentation():
    doc_context = {
        'readme_content': '# My Charm\nA charm for things.',
        'doc_files': ['docs/tutorial.md', 'docs/reference.md'],
        'documentation_url': 'https://docs.example.com',
    }

    backend = _make_mock_backend('Needs work: missing usage examples.')
    result = asyncio.run(assess_documentation(backend, doc_context))

    assert 'usage examples' in result
    # Verify the backend was called with the right system prompt.
    backend.send_message.assert_called_once()
    prompt_arg = backend.send_message.call_args[0][1]
    assert 'My Charm' in prompt_arg
    assert 'docs/tutorial.md' in prompt_arg


def test_assess_metadata():
    charmcraft_data = {
        'name': 'my-charm',
        'title': 'My Charm',
        'summary': 'A test charm.',
        'description': 'This charm does things.',
    }

    backend = _make_mock_backend('- Summary: Good.\n- Description: Needs more detail.')
    result = asyncio.run(assess_metadata(backend, charmcraft_data))

    assert 'Description' in result
    prompt_arg = backend.send_message.call_args[0][1]
    assert 'My Charm' in prompt_arg


def test_assess_metadata_empty():
    backend = _make_mock_backend()
    result = asyncio.run(assess_metadata(backend, {}))
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


def test_generate_summary_with_metadata():
    results = [_make_result('check_a', passed=True, description='* [x] Check A.')]
    metadata = {'name': 'my-charm', 'title': 'My Charm', 'summary': 'A test charm.'}

    backend = _make_mock_backend('All checks pass.')
    summary = asyncio.run(
        ai_client.generate_summary(backend, 'my-charm', results, charmcraft_data=metadata)
    )

    assert summary == 'All checks pass.'
    # Verify metadata was included in the prompt.
    prompt_arg = backend.send_message.call_args[0][1]
    assert 'My Charm' in prompt_arg
