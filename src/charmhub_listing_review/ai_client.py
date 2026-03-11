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

"""AI-powered review features using the GitHub Copilot SDK.

This module provides the integration layer between the listing review tool
and the GitHub Copilot SDK. All AI functionality is optional — when the SDK
or Copilot CLI is not available, the tool falls back to its standard behavior.
"""

from __future__ import annotations

import functools
import shutil

_copilot_available: bool | None = None


def is_ai_available() -> bool:
    """Check whether the Copilot SDK and CLI are available.

    The result is cached after the first call.
    """
    global _copilot_available
    if _copilot_available is not None:
        return _copilot_available
    _copilot_available = _check_ai_available()
    return _copilot_available


def _check_ai_available() -> bool:
    """Perform the actual availability check.

    Both the ``github-copilot-sdk`` Python package *and* the ``copilot`` CLI
    tool must be present. The SDK communicates with the CLI over JSON-RPC —
    the CLI handles authentication and token management, while the SDK
    provides the Python async API on top of it.
    """
    try:
        import copilot  # noqa: F401
    except ImportError:
        return False
    if not shutil.which('copilot'):
        return False
    return True


@functools.cache
def _get_client():
    """Get or create the shared CopilotClient instance."""
    from copilot import CopilotClient

    return CopilotClient()


async def start_client():
    """Start the shared Copilot client."""
    client = _get_client()
    await client.start()
    return client


async def stop_client():
    """Stop the shared Copilot client."""
    client = _get_client()
    await client.stop()


async def create_session(system_message: str, **kwargs):
    """Create a new Copilot session with the given system message.

    Args:
        system_message: The system prompt to use for this session.
        **kwargs: Additional session configuration (e.g. tools, streaming).

    Returns:
        A CopilotSession instance.
    """
    client = _get_client()
    config = {
        # gpt-4.1 is the recommended model for the Copilot SDK — it offers
        # the best balance of quality and speed for code-related tasks.
        'model': 'gpt-4.1',
        'systemMessage': {'content': system_message},
        **kwargs,
    }
    return await client.create_session(config)


async def send_prompt(session, prompt: str) -> str:
    """Send a prompt to a session and return the text response.

    Args:
        session: A CopilotSession instance.
        prompt: The prompt text to send.

    Returns:
        The assistant's response text, or an empty string if no response.
    """
    response = await session.send_and_wait({'prompt': prompt})
    if response and response.data and response.data.content:
        return response.data.content
    return ''


def print_ai_unavailable_notice():
    """Print a notice that AI features are disabled."""
    print(
        '\nNote: AI-powered features are disabled (Copilot SDK not available).'
        '\n      Install with: uv sync --group ai'
    )
