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

"""Reading GitHub Actions workflows, and working out when they actually run.

Two checklist items - "automated releasing to unstable channels exists" and
"integration tests are run on every change to the default branch" - both turn
on *when a workflow runs*, not on what it contains. That is a property of the
``on:`` block, so it is exact and needs no model. Grepping for
``charmcraft upload`` answers "yes, this charm releases automatically" for a
repository whose only publishing workflow is ``workflow_dispatch``, which is
the wrong answer.

The complication is ``workflow_call``: a reusable workflow has no triggers of
its own and runs whenever something calls it, so its effective triggers are
the union of its callers'. :func:`resolve_triggers` follows those edges.
"""

from __future__ import annotations

import dataclasses
import fnmatch
import pathlib
import re
from collections.abc import Iterator
from typing import Any

import yaml

WORKFLOW_DIR = '.github/workflows'

# YAML 1.1 - which is what PyYAML implements - resolves an unquoted `on` to the
# boolean True. A workflow file's top-level `on:` key therefore arrives as
# `True` unless its author happened to quote it, and both spellings are common.
_ON_KEYS = ('on', True)

# Branch patterns that match whatever the default branch is called.
_MATCH_ALL_BRANCHES = ('*', '**')


@dataclasses.dataclass
class Workflow:
    """One ``.github/workflows/*.yaml`` file."""

    path: str
    """Repository-relative path, e.g. ``.github/workflows/ci.yaml``."""

    name: str
    """The workflow's ``name:``, falling back to its file name."""

    triggers: dict[str, Any] = dataclasses.field(default_factory=dict)
    """The ``on:`` block, normalised to a mapping of event name to filters.

    The shorthand forms (``on: push`` and ``on: [push, pull_request]``) carry
    no filters, and are normalised to ``{'push': None}`` and so on.
    """

    jobs: dict[str, Any] = dataclasses.field(default_factory=dict)
    """The ``jobs:`` block, as parsed."""

    unreadable: str = ''
    """Why the file could not be read, if it could not."""


@dataclasses.dataclass
class TriggerSummary:
    """When a workflow actually runs."""

    events: set[str] = dataclasses.field(default_factory=set)
    """Effective event names, resolved through ``workflow_call``."""

    default_branch_push: bool = False
    """Whether a push to the default branch runs this workflow."""

    caveats: list[str] = dataclasses.field(default_factory=list)
    """Reasons the answer above is narrower than it looks, e.g. path filters."""

    called_by: list[str] = dataclasses.field(default_factory=list)
    """Workflows that reach this one through ``workflow_call``."""


def load_workflows(repo_path: pathlib.Path) -> list[Workflow]:
    """Read every workflow in ``.github/workflows``.

    ``repo_path`` is the repository root, not the charm directory: a monorepo
    keeps one workflow directory for charms that live in subdirectories.
    """
    workflow_dir = repo_path / WORKFLOW_DIR
    if not workflow_dir.is_dir():
        return []

    workflows: list[Workflow] = []
    for path in sorted(workflow_dir.iterdir()):
        if path.suffix not in ('.yaml', '.yml') or not path.is_file():
            continue
        relative = path.relative_to(repo_path).as_posix()
        try:
            data = yaml.safe_load(path.read_text(encoding='utf-8', errors='replace'))
        except (yaml.YAMLError, OSError) as exc:
            workflows.append(Workflow(path=relative, name=path.name, unreadable=str(exc)))
            continue
        if not isinstance(data, dict):
            workflows.append(
                Workflow(path=relative, name=path.name, unreadable='not a YAML mapping')
            )
            continue
        jobs = data.get('jobs')
        workflows.append(
            Workflow(
                path=relative,
                name=str(data.get('name') or path.name),
                triggers=_normalise_triggers(data),
                jobs=jobs if isinstance(jobs, dict) else {},
            )
        )
    return workflows


def _normalise_triggers(data: dict[Any, Any]) -> dict[str, Any]:
    """Normalise the three spellings of ``on:`` to a mapping."""
    raw = None
    for key in _ON_KEYS:
        if key in data:
            raw = data[key]
            break
    if isinstance(raw, str):
        return {raw: None}
    if isinstance(raw, list):
        return {str(event): None for event in raw}
    if isinstance(raw, dict):
        return {str(event): filters for event, filters in raw.items()}
    return {}


def _push_covers_default_branch(
    filters: Any,
    default_branch: str,
) -> tuple[bool, str]:
    """Does this ``push:`` filter block fire for the default branch?

    Returns whether it fires, plus a caveat when it fires for only some
    changes - a ``paths:`` filter still means the workflow does not run on
    *every* change to the branch, which is what the checklist asks for.
    """
    if not isinstance(filters, dict):
        # `push:` with no filters at all runs on every branch.
        return True, ''

    if _has_filter(filters, 'branches-ignore'):
        ignored = _as_list(filters['branches-ignore'])
        if any(_branch_matches(pattern, default_branch) for pattern in ignored):
            return False, ''
    elif _has_filter(filters, 'branches'):
        if not _branches_cover(_as_list(filters['branches']), default_branch):
            return False, ''

    for key in ('paths', 'paths-ignore'):
        if _has_filter(filters, key):
            return True, f'its push trigger has a {key} filter, so some changes do not run it'
    return True, ''


def _has_filter(filters: dict[Any, Any], key: str) -> bool:
    return key in filters and filters[key] is not None


def _as_list(value: Any) -> list[str]:
    if isinstance(value, str):
        return [value]
    if isinstance(value, list):
        return [str(item) for item in value]
    return []


def _branches_cover(patterns: list[str], branch: str) -> bool:
    """Does a ``branches:`` list fire for *branch*?

    GitHub evaluates positive patterns first and then lets a ``!`` pattern
    take a branch back out again, so ``['**', '!main']`` is the ordinary way
    to write "every branch except the default one". Matching the whole list
    positionally instead reads that as a match on `main`, which is a tick for
    a workflow that GitHub never runs there.
    """
    positive = [pattern for pattern in patterns if not pattern.startswith('!')]
    negative = [pattern[1:] for pattern in patterns if pattern.startswith('!')]
    if not any(_branch_matches(pattern, branch) for pattern in positive):
        return False
    return not any(_branch_matches(pattern, branch) for pattern in negative)


def _branch_matches(pattern: str, branch: str) -> bool:
    if pattern in _MATCH_ALL_BRANCHES:
        return True
    # fnmatch's `*` matches a `/` and GitHub's does not, so a pattern like
    # `release/*` is read more broadly here than GitHub reads it. That only
    # matters for a default branch with a slash in its name, which is rare
    # enough to leave alone.
    return fnmatch.fnmatchcase(branch, pattern)


def _called_workflows(workflow: Workflow) -> set[str]:
    """Local workflows this one invokes with ``jobs.<id>.uses``.

    Only ``./.github/workflows/...`` references resolve to a file we can read;
    a ``uses:`` pointing at another repository is somebody else's workflow.
    """
    called: set[str] = set()
    for job in workflow.jobs.values():
        if not isinstance(job, dict):
            continue
        uses = job.get('uses')
        if isinstance(uses, str) and uses.startswith('./'):
            called.add(uses.removeprefix('./').split('@')[0])
    return called


def resolve_triggers(
    workflows: list[Workflow],
    default_branch: str,
) -> dict[str, TriggerSummary]:
    """Work out when each workflow runs, following ``workflow_call`` edges.

    A workflow whose only trigger is ``workflow_call`` runs exactly when its
    callers run, so its summary is the union of theirs. Chains are followed to
    a fixed point; a cycle - which GitHub rejects anyway - simply stops adding.
    """
    summaries: dict[str, TriggerSummary] = {}
    for workflow in workflows:
        summary = TriggerSummary(events=set(workflow.triggers))
        if 'push' in workflow.triggers:
            covers, caveat = _push_covers_default_branch(workflow.triggers['push'], default_branch)
            summary.default_branch_push = covers
            if caveat:
                summary.caveats.append(caveat)
        summaries[workflow.path] = summary

    callers: dict[str, set[str]] = {workflow.path: set() for workflow in workflows}
    for workflow in workflows:
        for called in _called_workflows(workflow):
            if called in callers:
                callers[called].add(workflow.path)

    # Propagate until nothing changes: a caller may itself be a called workflow.
    for _ in range(len(workflows) + 1):
        changed = False
        for path, summary in summaries.items():
            if 'workflow_call' not in summary.events:
                continue
            for caller in callers[path]:
                caller_summary = summaries[caller]
                inherited = caller_summary.events - {'workflow_call'}
                if not inherited <= summary.events:
                    summary.events |= inherited
                    changed = True
                if caller_summary.default_branch_push and not summary.default_branch_push:
                    summary.default_branch_push = True
                    changed = True
                if caller not in summary.called_by:
                    summary.called_by.append(caller)
                    changed = True
                for caveat in caller_summary.caveats:
                    if caveat not in summary.caveats:
                        summary.caveats.append(caveat)
                        changed = True
        if not changed:
            break
    return summaries


def describe_triggers(summary: TriggerSummary) -> str:
    """A short human-readable description of when a workflow runs."""
    events = ', '.join(sorted(summary.events)) or 'nothing'
    if summary.called_by:
        events += f' (via {", ".join(sorted(summary.called_by))})'
    return events


def iter_steps(workflow: Workflow) -> Iterator[tuple[str, dict[str, Any]]]:
    """Yield ``(job_id, step)`` for every step in the workflow."""
    for job_id, job in workflow.jobs.items():
        if not isinstance(job, dict):
            continue
        steps = job.get('steps')
        if not isinstance(steps, list):
            continue
        for step in steps:
            if isinstance(step, dict):
                yield str(job_id), step


def iter_external_uses(workflow: Workflow) -> Iterator[tuple[str, str]]:
    """Yield ``(job_id, uses)`` for reusable workflows from other repositories.

    Their contents are not in this repository, so anything they do - releasing
    a charm, running a test suite - is invisible to a static read.
    """
    for job_id, job in workflow.jobs.items():
        if not isinstance(job, dict):
            continue
        uses = job.get('uses')
        if isinstance(uses, str) and not uses.startswith('./'):
            yield str(job_id), uses


def _joined_lines(run: str) -> list[str]:
    r"""Split a ``run:`` block into commands, honouring ``\\`` continuations.

    Release workflows carry a lot of flags, so the invocation a consumer wants
    to match (``charmcraft upload ... --release``) is routinely spread over
    several lines. Splitting on newlines alone hands every consumer fragments
    and makes them each solve this.
    """
    lines: list[str] = []
    pending = ''
    for raw in run.splitlines():
        stripped = raw.strip()
        if stripped.endswith('\\'):
            pending += stripped[:-1].rstrip() + ' '
            continue
        lines.append((pending + stripped).strip())
        pending = ''
    if pending:
        lines.append(pending.strip())
    return lines


def step_commands(workflow: Workflow) -> list[tuple[str, str]]:
    """Yield ``(location, command line)`` for every shell line the steps run."""
    commands: list[tuple[str, str]] = []
    for job_id, step in iter_steps(workflow):
        run = step.get('run')
        if not isinstance(run, str):
            continue
        location = f'{workflow.path} ({job_id})'
        commands.extend(
            (location, line) for line in _joined_lines(run) if line and not line.startswith('#')
        )
    return commands


def matches_any(text: str, patterns: tuple[re.Pattern[str], ...]) -> bool:
    return any(pattern.search(text) for pattern in patterns)
