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
import enum
from typing import Any


class Verdict(enum.Enum):
    """The outcome of assessing a single checklist item.

    ``CheckResult.passed`` uses ``bool | None`` and so cannot distinguish
    "we could not tell" from "this item does not apply to this charm". Both
    render as an unticked box, which asks the reviewer to go and look at
    something that may have nothing to look at. Item assessments use this
    four-valued vocabulary instead.
    """

    PASS = 'pass'  # noqa: S105 (a verdict, not a password)
    """The charm meets the requirement."""

    FAIL = 'fail'
    """The charm does not meet the requirement."""

    NOT_APPLICABLE = 'not-applicable'
    """The requirement does not apply to this charm.

    For example, an item about binary resource architectures when the charm
    publishes no binary resources. This is a decision, not an absence of one:
    the reviewer does not need to look.
    """

    NEEDS_HUMAN = 'needs-human'
    """The evidence was gathered but is not conclusive."""


@dataclasses.dataclass
class ItemAssessment:
    """The outcome of assessing one checklist item, with its reasoning."""

    checklist_id: str
    """The ``<!-- id: ... -->`` slug of the item this assesses."""

    verdict: Verdict
    """The assessment outcome."""

    rationale: str
    """One sentence explaining the verdict, shown to the reviewer."""

    evidence: list[str] = dataclasses.field(default_factory=list)
    """Human-readable evidence lines, e.g. ``'src/charm.py:466: ...'``.

    These are what the reviewer checks the verdict against, and what an AI
    backend is given when the deterministic pass returns ``NEEDS_HUMAN``.
    """


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
