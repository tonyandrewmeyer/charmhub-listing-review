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

"""GitHub Copilot SDK backend for AI-powered review features."""

from __future__ import annotations

import shutil


class CopilotSession:
    """Multi-turn conversation session backed by the Copilot SDK."""

    def __init__(self, session):
        self._session = session

    async def send(self, prompt: str) -> str:
        """Send a prompt and return the response text."""
        response = await self._session.send_and_wait({'prompt': prompt})
        if response and response.data and response.data.content:
            return response.data.content
        return ''


class CopilotBackend:
    """AI backend using the GitHub Copilot SDK.

    Requires both the ``github-copilot-sdk`` Python package and the
    ``copilot`` CLI tool to be installed.
    """

    def __init__(self):
        self._client = None

    def is_available(self) -> bool:
        """Check whether the Copilot SDK and CLI are available."""
        try:
            import copilot  # noqa: F401  # ty: ignore[unresolved-import]
        except ImportError:
            return False
        if not shutil.which('copilot'):
            return False
        return True

    async def start(self) -> None:
        """Start the Copilot client."""
        from copilot import CopilotClient  # ty: ignore[unresolved-import]

        self._client = CopilotClient()
        await self._client.start()

    async def stop(self) -> None:
        """Stop the Copilot client."""
        if self._client is not None:
            await self._client.stop()
            self._client = None

    async def send_message(self, system_message: str, prompt: str) -> str:
        """Send a one-shot prompt with a system message."""
        session = await self.create_session(system_message)
        return await session.send(prompt)

    async def create_session(self, system_message: str) -> CopilotSession:
        """Create a multi-turn Copilot session."""
        if self._client is None:
            msg = 'Backend not started. Call start() first.'
            raise RuntimeError(msg)
        config = {
            'model': 'gpt-4.1',
            'systemMessage': {'content': system_message},
        }
        raw_session = await self._client.create_session(config)
        return CopilotSession(raw_session)
