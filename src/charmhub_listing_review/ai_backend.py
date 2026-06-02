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

    def unavailability_reason(self) -> str | None:
        """Return a human-readable reason why this backend is unavailable, or None if available."""
        ...

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


def resolve_backend(choice: str = 'auto') -> tuple[AIBackend | None, str]:
    """Resolve which AI backend to use.

    Args:
        choice: One of ``'copilot'``, ``'snap'``, ``'auto'``, or ``'none'``.
            ``'auto'`` tries Copilot first, then snap.
            The ``CHARMHUB_REVIEW_AI_BACKEND`` environment variable is used
            as a fallback when *choice* is ``'auto'``.

    Returns:
        A ``(backend, reason)`` tuple.  When a backend is available *reason*
        is an empty string.  When no backend is available *backend* is ``None``
        and *reason* is a human-readable explanation.  *reason* is also empty
        when *choice* is ``'none'`` (AI intentionally disabled).
    """
    if choice == 'auto':
        choice = os.environ.get('CHARMHUB_REVIEW_AI_BACKEND', 'auto')

    if choice == 'none':
        return None, ''

    if choice == 'copilot':
        backend = copilot_backend.CopilotBackend()
        reason = backend.unavailability_reason()
        if reason is None:
            return backend, ''
        return None, f'Copilot backend unavailable: {reason}'

    if choice == 'snap':
        backend = snap_backend.SnapBackend()
        reason = backend.unavailability_reason()
        if reason is None:
            return backend, ''
        return None, f'Snap backend unavailable: {reason}'

    # auto: try copilot, then snap.
    copilot = copilot_backend.CopilotBackend()
    copilot_reason = copilot.unavailability_reason()
    if copilot_reason is None:
        return copilot, ''

    snap = snap_backend.SnapBackend()
    snap_reason = snap.unavailability_reason()
    if snap_reason is None:
        return snap, ''

    return None, f'No AI backend is available.\n  Copilot: {copilot_reason}\n  Snap: {snap_reason}'
