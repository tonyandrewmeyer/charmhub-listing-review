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

"""Test the AI backend abstraction and implementations."""

import asyncio
from unittest import mock

from charmhub_listing_review import ai_backend, copilot_backend, snap_backend


class TestResolveBackend:
    def test_auto_returns_none_when_nothing_available(self):
        with (
            mock.patch.object(copilot_backend.CopilotBackend, 'is_available', return_value=False),
            mock.patch.object(snap_backend.SnapBackend, 'is_available', return_value=False),
            mock.patch.dict('os.environ', {}, clear=True),
        ):
            result = ai_backend.resolve_backend('auto')
        assert result is None

    def test_auto_prefers_copilot(self):
        with (
            mock.patch.object(copilot_backend.CopilotBackend, 'is_available', return_value=True),
            mock.patch.dict('os.environ', {}, clear=True),
        ):
            result = ai_backend.resolve_backend('auto')
        assert isinstance(result, copilot_backend.CopilotBackend)

    def test_auto_falls_back_to_snap(self):
        with (
            mock.patch.object(copilot_backend.CopilotBackend, 'is_available', return_value=False),
            mock.patch.object(snap_backend.SnapBackend, 'is_available', return_value=True),
            mock.patch.dict('os.environ', {}, clear=True),
        ):
            result = ai_backend.resolve_backend('auto')
        assert isinstance(result, snap_backend.SnapBackend)

    def test_explicit_copilot(self):
        with mock.patch.object(copilot_backend.CopilotBackend, 'is_available', return_value=True):
            result = ai_backend.resolve_backend('copilot')
        assert isinstance(result, copilot_backend.CopilotBackend)

    def test_explicit_copilot_unavailable(self):
        with mock.patch.object(copilot_backend.CopilotBackend, 'is_available', return_value=False):
            result = ai_backend.resolve_backend('copilot')
        assert result is None

    def test_explicit_snap(self):
        with mock.patch.object(snap_backend.SnapBackend, 'is_available', return_value=True):
            result = ai_backend.resolve_backend('snap')
        assert isinstance(result, snap_backend.SnapBackend)

    def test_explicit_snap_unavailable(self):
        with mock.patch.object(snap_backend.SnapBackend, 'is_available', return_value=False):
            result = ai_backend.resolve_backend('snap')
        assert result is None

    def test_env_var_overrides_auto(self):
        with (
            mock.patch.dict('os.environ', {'CHARMHUB_REVIEW_AI_BACKEND': 'snap'}),
            mock.patch.object(snap_backend.SnapBackend, 'is_available', return_value=True),
        ):
            result = ai_backend.resolve_backend('auto')
        assert isinstance(result, snap_backend.SnapBackend)


class TestCopilotBackend:
    @mock.patch('charmhub_listing_review.copilot_backend.shutil.which', return_value=None)
    def test_not_available_no_cli(self, _mock_which):
        backend = copilot_backend.CopilotBackend()
        assert backend.is_available() is False

    @mock.patch(
        'charmhub_listing_review.copilot_backend.shutil.which',
        return_value='/usr/bin/copilot',
    )
    def test_not_available_no_sdk(self, _mock_which):
        with mock.patch.dict('sys.modules', {'copilot': None}):
            backend = copilot_backend.CopilotBackend()
            assert backend.is_available() is False

    def test_create_session_without_start_raises(self):
        backend = copilot_backend.CopilotBackend()
        with mock.patch.dict('sys.modules', {'copilot': mock.MagicMock()}):
            try:
                asyncio.run(backend.create_session('system prompt'))
                raise AssertionError('Expected RuntimeError')
            except RuntimeError as e:
                assert 'start()' in str(e)


class TestSnapBackend:
    def test_not_available_no_openai(self):
        with mock.patch.dict('sys.modules', {'openai': None}):
            backend = snap_backend.SnapBackend()
            assert backend.is_available() is False

    def test_not_available_no_endpoint(self):
        with (
            mock.patch.dict('sys.modules', {'openai': mock.MagicMock()}),
            mock.patch.object(snap_backend.SnapBackend, '_discover_endpoint', return_value=None),
        ):
            backend = snap_backend.SnapBackend()
            assert backend.is_available() is False

    def test_available_with_endpoint(self):
        with (
            mock.patch.dict('sys.modules', {'openai': mock.MagicMock()}),
            mock.patch.object(
                snap_backend.SnapBackend,
                '_discover_endpoint',
                return_value='http://localhost:8326/v3',
            ),
        ):
            backend = snap_backend.SnapBackend()
            assert backend.is_available() is True

    def test_create_session_without_start_raises(self):
        backend = snap_backend.SnapBackend()
        try:
            asyncio.run(backend.create_session('system prompt'))
            raise AssertionError('Expected RuntimeError')
        except RuntimeError as e:
            assert 'start()' in str(e)

    def test_snap_session_accumulates_messages(self):
        mock_client = mock.AsyncMock()
        mock_response = mock.Mock()
        mock_response.choices = [mock.Mock()]
        mock_response.choices[0].message.content = 'response text'
        mock_client.chat.completions.create.return_value = mock_response

        session = snap_backend.SnapSession(mock_client, 'test-model', 'system msg')

        result = asyncio.run(session.send('hello'))
        assert result == 'response text'

        # After the first send, session should have: system + user + assistant.
        assert len(session._messages) == 3
        assert session._messages[0] == {'role': 'system', 'content': 'system msg'}
        assert session._messages[1] == {'role': 'user', 'content': 'hello'}
        assert session._messages[2] == {'role': 'assistant', 'content': 'response text'}

        # Second send should include full history.
        result = asyncio.run(session.send('follow up'))
        assert len(session._messages) == 5  # system + user + assistant + user + assistant

    def test_probe_handles_connection_error(self):
        assert snap_backend.SnapBackend._probe('http://localhost:99999/v3') is False
