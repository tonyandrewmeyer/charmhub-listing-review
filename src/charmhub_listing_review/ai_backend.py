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

"""Backend abstraction for AI-powered review features.

Defines the protocol that AI backends must implement, and provides the
``resolve_backend`` function to select the appropriate backend based on
user configuration and availability.
"""

from __future__ import annotations

import os
import typing

from . import copilot_backend, snap_backend


class AISession(typing.Protocol):
    """A stateful multi-turn conversation session."""

    async def send(self, prompt: str) -> str:
        """Send a prompt and return the response text."""
        ...


class AIBackend(typing.Protocol):
    """Protocol for AI inference backends."""

    def is_available(self) -> bool:
        """Check whether this backend is ready to use."""
        ...

    async def start(self) -> None:
        """Start the backend (connect, authenticate, etc.)."""
        ...

    async def stop(self) -> None:
        """Stop the backend and release resources."""
        ...

    async def send_message(self, system_message: str, prompt: str) -> str:
        """Send a one-shot prompt with a system message and return the response."""
        ...

    async def create_session(self, system_message: str) -> AISession:
        """Create a multi-turn session with the given system message."""
        ...


def resolve_backend(choice: str = 'auto') -> AIBackend | None:
    """Resolve which AI backend to use.

    Args:
        choice: One of ``'copilot'``, ``'snap'``, or ``'auto'``.
            ``'auto'`` tries Copilot first, then snap, then returns None.
            The ``CHARMHUB_REVIEW_AI_BACKEND`` environment variable is used
            as a fallback when *choice* is ``'auto'``.

    Returns:
        An ``AIBackend`` instance, or ``None`` if no backend is available.
    """
    if choice == 'auto':
        choice = os.environ.get('CHARMHUB_REVIEW_AI_BACKEND', 'auto')

    if choice == 'none':
        return None

    if choice == 'copilot':
        backend = copilot_backend.CopilotBackend()
        return backend if backend.is_available() else None

    if choice == 'snap':
        backend = snap_backend.SnapBackend()
        return backend if backend.is_available() else None

    # auto: try copilot, then snap.
    copilot = copilot_backend.CopilotBackend()
    if copilot.is_available():
        return copilot

    snap = snap_backend.SnapBackend()
    if snap.is_available():
        return snap

    return None


def print_ai_unavailable_notice():
    """Print a notice that AI features are disabled."""
    print(
        '\nNote: AI-powered features are disabled (no AI backend available).'
        '\n      Copilot: install with: uv sync --group ai'
        '\n      Snap:    install an inference snap, e.g. sudo snap install gemma3'
    )
