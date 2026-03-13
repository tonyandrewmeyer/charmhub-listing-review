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

"""Data models shared across modules."""

import dataclasses
from typing import Any


@dataclasses.dataclass
class CheckResult:
    """Result of a single automated check."""

    name: str
    """Identifier for the check, e.g. 'license_statement'."""

    passed: bool | None
    """True=pass, False=fail, None=could not be determined automatically."""

    description: str
    """The markdown checklist line, e.g. '* [x] The charm has an icon.'"""

    context: dict[str, Any] = dataclasses.field(default_factory=dict)
    """Extra data for AI analysis (e.g. {"url": "...", "status_code": 404})."""

    ai_explanation: str = ''
    """AI-generated explanation for failed checks, populated by ai_client."""


@dataclasses.dataclass
class EvaluationResult:
    """Complete evaluation result including check results and repo context."""

    checks: list[CheckResult]
    """The individual check results."""

    charmcraft_data: dict[str, Any] | None = None
    """Parsed charmcraft.yaml data, if available."""

    doc_context: dict[str, Any] = dataclasses.field(default_factory=dict)
    """Documentation context for AI quality assessment."""

    code_context: dict[str, Any] = dataclasses.field(default_factory=dict)
    """Code context for AI code quality analysis."""
