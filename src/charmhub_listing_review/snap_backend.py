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

"""Canonical inference snap backend for AI-powered review features.

Uses the OpenAI-compatible API exposed by Canonical inference snaps
(e.g. ``gemma3``, ``deepseek-r1``, ``nemotron-3-nano``).
"""

from __future__ import annotations

import os
import urllib.error
import urllib.request

# Default ports used by common inference snaps.  When no explicit URL is
# configured we probe these in order to auto-discover a running snap.
_DEFAULT_PORTS = (8326, 8327, 8328, 8329, 8330)

_DEFAULT_BASE_URL = 'http://localhost:8326/v3'


class SnapSession:
    """Multi-turn conversation session backed by the OpenAI-compatible API."""

    def __init__(self, client, model: str, system_message: str):
        self._client = client
        self._model = model
        self._messages: list[dict[str, str]] = [
            {'role': 'system', 'content': system_message},
        ]

    async def send(self, prompt: str) -> str:
        """Send a prompt, accumulating conversation history."""
        self._messages.append({'role': 'user', 'content': prompt})
        response = await self._client.chat.completions.create(
            model=self._model,
            messages=self._messages,
        )
        content = response.choices[0].message.content or ''
        self._messages.append({'role': 'assistant', 'content': content})
        return content


class SnapBackend:
    """AI backend using a Canonical inference snap's OpenAI-compatible API.

    Configuration is via environment variables:

    - ``SNAP_API_URL``: base URL of the snap API
      (default: auto-discovered or ``http://localhost:8326/v3``)
    - ``SNAP_MODEL``: model name to use
      (default: auto-discovered via ``GET /models``)
    """

    def __init__(self):
        self._client = None
        self._model: str = ''
        self._base_url: str = os.environ.get('SNAP_API_URL', '')

    def is_available(self) -> bool:
        """Check whether an inference snap API is reachable."""
        try:
            import openai  # noqa: F401  # ty: ignore[unresolved-import]
        except ImportError:
            return False
        return self._discover_endpoint() is not None

    async def start(self) -> None:
        """Initialise the async OpenAI client."""
        import openai  # ty: ignore[unresolved-import]

        base_url = self._base_url or self._discover_endpoint() or _DEFAULT_BASE_URL
        self._client = openai.AsyncOpenAI(
            base_url=base_url,
            api_key='not-needed',
        )
        self._model = os.environ.get('SNAP_MODEL', '') or await self._discover_model()

    async def stop(self) -> None:
        """Release the client."""
        if self._client is not None:
            await self._client.close()
            self._client = None

    async def send_message(self, system_message: str, prompt: str) -> str:
        """Send a one-shot prompt with a system message."""
        if self._client is None:
            msg = 'Backend not started. Call start() first.'
            raise RuntimeError(msg)
        response = await self._client.chat.completions.create(
            model=self._model,
            messages=[
                {'role': 'system', 'content': system_message},
                {'role': 'user', 'content': prompt},
            ],
        )
        return response.choices[0].message.content or ''

    async def create_session(self, system_message: str) -> SnapSession:
        """Create a multi-turn session."""
        if self._client is None:
            msg = 'Backend not started. Call start() first.'
            raise RuntimeError(msg)
        return SnapSession(self._client, self._model, system_message)

    def _discover_endpoint(self) -> str | None:
        """Probe common snap ports to find a running inference snap."""
        if self._base_url:
            return self._base_url if self._probe(self._base_url) else None

        for port in _DEFAULT_PORTS:
            # Inference snaps commonly use /v3 as the base path.
            for base_path in ('/v3', '/v1'):
                url = f'http://localhost:{port}{base_path}'
                if self._probe(url):
                    self._base_url = url
                    return url
        return None

    @staticmethod
    def _probe(url: str) -> bool:
        """Check if the given URL responds to a GET /models request."""
        try:
            req = urllib.request.Request(  # noqa: S310
                f'{url}/models', method='GET'
            )
            with urllib.request.urlopen(req, timeout=2) as resp:  # noqa: S310
                return resp.status == 200
        except (urllib.error.URLError, OSError, TimeoutError, ValueError, OverflowError):
            return False

    async def _discover_model(self) -> str:
        """Query the snap API for available models and pick the first one."""
        if self._client is None:
            return ''
        try:
            models = await self._client.models.list()
            if models.data:
                return models.data[0].id
        except Exception:  # noqa: S110
            pass
        return ''
