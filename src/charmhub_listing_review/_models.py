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
    """Extra check-specific data (e.g. {"url": "...", "status_code": 404})."""

    checklist_id: str | None = None
    """The ID of the checklist item this check corresponds to.

    Matches `<!-- id: ... -->` markers in the rendered checklist. ``None``
    means this check has no matching checklist entry (yet) and won't auto-tick.
    """


@dataclasses.dataclass
class EvaluationResult:
    """Complete evaluation result."""

    checks: list[CheckResult]
    """The individual check results."""
