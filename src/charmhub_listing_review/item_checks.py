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

"""The shape a checklist item's assessment takes, whatever decided it.

``evaluate.py`` holds the checks that decide an item from metadata: does a
file exist, does a URL resolve, does a YAML key have the right shape. The
items that remain unticked need more than that, and three things answer them:

* **charmlint**, over the charm's YAML, under a listing-review configuration.
* **ruff**, over the charm's Python, likewise.
* **an AI pass**, for the items neither linter can decide - and for reading
  what the author chose to suppress, since a ``# noqa`` is an answer of a
  kind.

This module does not implement any of those. It holds what they have in
common: a per-item verdict with the evidence behind it, so a reviewer reads
one kind of answer rather than three, and so the renderers do not care which
layer produced a given tick.

Each item is an :class:`ItemCheck` with two halves:

* ``gather`` collects the evidence the item names, and nothing else -
  normally by running a linter and keeping the diagnostics for this item. It
  is deliberately separate so that the same evidence feeds both halves below,
  and so that what an AI backend sees is reviewable without running one.
* ``decide`` rules on that evidence deterministically, returning
  :attr:`~._models.Verdict.NEEDS_HUMAN` for the residue it cannot settle.

Only that residue is worth an AI call, and it is smaller than it looks: most
of these items have a large mechanical core and a small judgement tail. An
item that returns ``NEEDS_HUMAN`` carries its gathered evidence with it, so
the backend is given the same material a reviewer would read rather than the
whole repository.
"""

from __future__ import annotations

import ast
import dataclasses
import json
import pathlib
import re
from collections.abc import Callable
from typing import Any
from urllib.parse import urlparse

import yaml

from . import _workflows
from ._models import ItemAssessment, Verdict
from .evaluate import effective_plugin

# Vendored charm libraries live here. They are third-party code that the charm
# author did not write and cannot change, so findings in them are not the
# charm's to answer for - see ``first_party_python_files``.
_LIB_CHARMS = ('lib', 'charms')

# Directories whose contents are not the charm's runtime behaviour.
#
# `.github` and `docs` were added after `charm-ubuntu` and `zookeeper-operator`
# real-charm validation runs turned up print()/logging findings inside a PR-title
# linter (`.github/check-conventional-pr-title.py`) and a Sphinx config helper
# (`docs/.sphinx/get_vale_conf.py`) respectively - CI- and doc-build-time
# tooling the charm author wrote, but never part of what Juju runs. `scripts/`
# is deliberately *not* here: postgresql-operator deploys `scripts/*.py` onto
# the target machine and runs it there (`src/rotate_logs.py` shells out to
# `scripts/rotate_logs.py`), so it is charm runtime behaviour under a
# different directory name, not tooling.
_EXCLUDED_DIRS = frozenset({
    'tests',
    'test',
    '.git',
    '.tox',
    'venv',
    '.venv',
    'build',
    '.github',
    'docs',
})


def first_party_python_files(
    charm_path: pathlib.Path,
    charm_name: str = '',
) -> list[pathlib.Path]:
    """Return the Python files the charm author is responsible for.

    A charm repository normally vendors its dependencies' charm libraries
    under ``lib/charms/<project>/vN/<library>.py``. Those are copies of other
    people's code: reporting findings in them tells the author about a problem
    in someone else's library, which they cannot act on, and which every charm
    vendoring that library would be told about too.

    The exception is the charm's *own* published libraries, under
    ``lib/charms/<this charm>/``, which are the author's to answer for.
    ``charm_name`` is matched with both hyphens and underscores, since the
    directory uses the underscored form.

    This answers "whose code is this?", not "what is wrong with it?" - the
    lint layers exclude vendored code through their own configuration. It is
    here for the AI pass, which is handed files to read and should not be
    handed somebody else's library.
    """
    own_lib_dirs: set[str] = set()
    if charm_name:
        own_lib_dirs = {charm_name.replace('-', '_'), charm_name.replace('_', '-')}

    files: list[pathlib.Path] = []
    for path in sorted(charm_path.rglob('*.py')):
        parts = path.relative_to(charm_path).parts
        if _EXCLUDED_DIRS.intersection(parts):
            continue
        # lib/charms/<project>/... - only ours counts.
        if parts[:2] == _LIB_CHARMS and (len(parts) < 3 or parts[2] not in own_lib_dirs):
            continue
        files.append(path)
    return files


@dataclasses.dataclass
class CharmSource:
    """Where to find the charm, and what to call it.

    The charm directory and the repository root are not always the same. A
    monorepo puts the charm in a subdirectory but keeps one ``.github`` at the
    top, so an item about CI reads the repository while an item about
    ``charmcraft.yaml`` reads the charm directory. Passing one path and
    guessing the other from it gets monorepos wrong in whichever direction the
    guess was made.
    """

    charm_path: pathlib.Path
    """The directory holding ``charmcraft.yaml``, and where the linters run."""

    repo_path: pathlib.Path | None = None
    """The repository root. Defaults to the charm directory."""

    charm_name: str = ''
    """The charm's name, used to tell its own library apart from vendored ones."""

    default_branch: str = 'main'
    """The repository's default branch, for "runs on every change to" items."""

    def __post_init__(self):
        if self.repo_path is None:
            self.repo_path = self.charm_path

    @property
    def repo(self) -> pathlib.Path:
        """The repository root, never ``None``."""
        assert self.repo_path is not None
        return self.repo_path

    def charmcraft_yaml(self) -> dict[str, Any]:
        """Parse ``charmcraft.yaml``, returning ``{}`` if it is missing or bad."""
        path = self.charm_path / 'charmcraft.yaml'
        if not path.is_file():
            return {}
        try:
            data = yaml.safe_load(path.read_text(encoding='utf-8', errors='replace'))
        except yaml.YAMLError:
            return {}
        return data if isinstance(data, dict) else {}


@dataclasses.dataclass
class Evidence:
    """What ``gather`` found, in a form both a human and a model can read."""

    lines: list[str] = dataclasses.field(default_factory=list)
    """Human-readable evidence, one line per finding or call site."""

    data: dict[str, Any] = dataclasses.field(default_factory=dict)
    """Structured evidence for ``decide`` to rule on."""

    unread: list[str] = dataclasses.field(default_factory=list)
    """Inputs this item needed and could not read, one line each.

    A file that would not parse is exactly where the answer might have been,
    so an item with anything here cannot be settled either way - see
    :meth:`ItemCheck.assess`, which enforces that so no ``decide`` has to
    remember to.
    """


@dataclasses.dataclass
class ItemCheck:
    """A checklist item and the two halves that answer it."""

    checklist_id: str
    """The ``<!-- id: ... -->`` slug this check answers."""

    gather: Callable[[CharmSource], Evidence]
    """Collect the evidence this item names, and nothing else."""

    decide: Callable[[Evidence], ItemAssessment]
    """Rule on gathered evidence, deferring what it cannot settle."""

    def assess(self, source: CharmSource) -> ItemAssessment:
        """Gather evidence for this item and rule on it.

        An item whose evidence names something it could not read is capped at
        ``NEEDS_HUMAN`` whatever ``decide`` concluded. A charm is not shown to
        be clean by a file that would not parse, and it is not shown to be
        broken by one either: the input that would have answered the question
        is the one that went missing. Enforcing it here rather than in each
        ``decide`` is deliberate - every item has this failure mode, and the
        ones that forget it are the ones that report a confident wrong answer.
        """
        evidence = self.gather(source)
        assessment = self.decide(evidence)
        if not evidence.unread or assessment.verdict is Verdict.NEEDS_HUMAN:
            return assessment
        return dataclasses.replace(
            assessment,
            verdict=Verdict.NEEDS_HUMAN,
            rationale=(
                f'{assessment.rationale.rstrip(".")}, but {len(evidence.unread)} input(s) '
                f'could not be read, so this is not settled.'
            ),
            evidence=assessment.evidence + evidence.unread,
        )


# --- best-practice-safe-subprocess -------------------------------------------

# The subprocess half of this item belongs to ruff, which already ships rules
# for it and, unlike a hand-rolled pass, resolves import bindings properly:
# S602/S604/S605 for shell interpretation, S607 for a relative program name,
# PLW1510 for `subprocess.run` without `check=`. Configure those in the
# listing-review ruff configuration rather than reimplementing them here.
#
# What is left is what ruff has no rule for, because it is an ops API rather
# than a standard-library one: `ops.Container.exec`. A Kubernetes charm runs
# everything through Pebble, so a charm can contain no `subprocess` call at
# all and still run commands throughout.
#
# Known gap: `subprocess.run` without `capture_output=` has no ruff rule
# either, and is not checked anywhere now. It wants a charmlint rule, or a
# ruff one upstream.

# Programs that take a command *string* and hand it to a shell, so that passing
# a list to exec() still ends up shell-interpreted.
_SHELL_WRAPPERS = frozenset({'sh', 'bash', 'dash', 'zsh', 'su', 'sudo', 'env'})


@dataclasses.dataclass
class _ExecSite:
    """One place the charm runs a command in its workload container."""

    location: str
    """``path:lineno``, relative to the charm directory."""

    problems: list[str] = dataclasses.field(default_factory=list)
    """Best-practice violations found at this site."""

    undecidable: str = ''
    """Why this site could not be ruled on, if it could not."""


class _ExecVisitor(ast.NodeVisitor):
    """Collect ``Container.exec`` call sites and what is wrong with them."""

    def __init__(self, relative_path: str):
        self._path = relative_path
        self.sites: list[_ExecSite] = []
        # Calls whose return value is discarded: the call is the whole
        # statement. `container.exec(...).wait_output()` does not appear here,
        # because there the outer wait_output() call is the statement.
        self._discarded: set[int] = set()
        # Local names that hold a literal command, innermost scope last.
        self._scopes: list[dict[str, ast.expr]] = []

    def visit_Module(self, node: ast.Module) -> None:
        self._visit_scope(node, node.body)

    def visit_FunctionDef(self, node: ast.FunctionDef) -> None:
        self._visit_scope(node, node.body)

    def visit_AsyncFunctionDef(self, node: ast.AsyncFunctionDef) -> None:
        self._visit_scope(node, node.body)

    def _visit_scope(self, node: ast.AST, body: list[ast.stmt]) -> None:
        self._push_scope(body)
        self.generic_visit(node)
        self._scopes.pop()

    def _push_scope(self, body: list[ast.stmt]) -> None:
        """Record names assigned exactly once from a literal in this scope.

        Charms routinely build the command a line or two before running it
        (``argv = ['su', 'git', '-c', cmd]``), so refusing to look through a
        local would defer most real call sites to a human. Resolving only
        single-assignment literals keeps that conservative: a name that is
        reassigned, or built by a call, stays unresolved.
        """
        assigned: dict[str, list[ast.expr]] = {}
        for statement in ast.walk(ast.Module(body=body, type_ignores=[])):
            if not isinstance(statement, ast.Assign):
                continue
            for target in statement.targets:
                if isinstance(target, ast.Name):
                    assigned.setdefault(target.id, []).append(statement.value)
        self._scopes.append({
            name: values[0]
            for name, values in assigned.items()
            if len(values) == 1
            and isinstance(values[0], (ast.List, ast.Tuple, ast.Constant, ast.JoinedStr))
        })

    def _resolve(self, node: ast.AST | None) -> ast.AST | None:
        """Follow a local name to the literal it was assigned, if it is one."""
        if not isinstance(node, ast.Name):
            return node
        for scope in reversed(self._scopes):
            if node.id in scope:
                return scope[node.id]
        return node

    def visit_Expr(self, node: ast.Expr) -> None:
        if isinstance(node.value, ast.Call):
            self._discarded.add(id(node.value))
        self.generic_visit(node)

    def visit_Call(self, node: ast.Call) -> None:
        """Handle ``<container>.exec([...])``.

        Any ``.exec()`` attribute call is treated as an ops ``Container.exec``.
        The name of the receiver varies too much across charms
        (``self.container``, ``self._container``, a local, a parameter) to
        match on, and no other common API spells a method ``exec``.
        """
        func = node.func
        if isinstance(func, ast.Attribute) and func.attr == 'exec':
            site = _ExecSite(location=self._location(node))
            _check_argv(site, self._resolve(node.args[0]) if node.args else None)
            if id(node) in self._discarded:
                site.problems.append(
                    'discards the ExecProcess without waiting, so a failure is never noticed'
                )
            self.sites.append(site)
        self.generic_visit(node)

    def _location(self, node: ast.AST) -> str:
        return f'{self._path}:{getattr(node, "lineno", 0)}'


def _check_argv(site: _ExecSite, argv: ast.AST | None) -> None:
    """Check the command argument for shell strings and relative paths."""
    if argv is None:
        site.undecidable = 'no positional command argument'
        return

    # Container.exec wants a list; a bare string is simply wrong.
    if isinstance(argv, ast.Constant) and isinstance(argv.value, str):
        site.problems.append('passes the command as a string rather than a list')
        _check_program(site, argv.value.split()[0] if argv.value.split() else '')
        return
    if isinstance(argv, ast.JoinedStr):
        site.problems.append('builds the command as an f-string rather than a list')
        return
    if not isinstance(argv, (ast.List, ast.Tuple)):
        site.undecidable = 'the command is built elsewhere, so its shape is not visible here'
        return

    elements = argv.elts
    if not elements:
        site.undecidable = 'empty command list'
        return

    first = elements[0]
    if not (isinstance(first, ast.Constant) and isinstance(first.value, str)):
        site.undecidable = 'the program name is not a literal'
        return
    _check_program(site, first.value)

    # `["su", "git", "-c", cmd]` is a list, but the shell still parses `cmd`.
    program = pathlib.PurePosixPath(first.value).name
    if program in _SHELL_WRAPPERS:
        for element in elements[1:]:
            if isinstance(element, ast.Constant) and element.value == '-c':
                site.problems.append(
                    f'runs a command string through {program}, so the shell interprets it'
                )
                break


# Callables that start an external process. Item 17's own check no longer
# reads these - that is ruff's - but item 16 still has to notice a workload
# being installed by shelling out.
_SUBPROCESS_FUNCTIONS = frozenset({'run', 'call', 'check_call', 'check_output', 'Popen'})


def _is_name(node: ast.AST, name: str) -> bool:
    """Is ``node`` a bare reference to ``name``?

    Only the dotted spelling is recognised, deliberately: this is a cheap
    match for `apt.add_package(...)`-shaped calls, not import resolution. A
    charm aliasing its imports defeats it, which is why the checks that have
    to be right about that are ruff's.
    """
    return isinstance(node, ast.Name) and node.id == name


def _check_program(site: _ExecSite, program: str) -> None:
    if program and not program.startswith('/'):
        site.problems.append(f'runs {program!r} by a relative name rather than an absolute path')


def gather_exec_sites(source: CharmSource) -> Evidence:
    """Collect every place the charm runs a command in its workload."""
    charm_path = source.charm_path
    sites: list[_ExecSite] = []
    unparsed: list[str] = []
    for path in first_party_python_files(charm_path, source.charm_name):
        relative = path.relative_to(charm_path).as_posix()
        try:
            tree = ast.parse(path.read_text(encoding='utf-8', errors='replace'))
        except (SyntaxError, ValueError):
            unparsed.append(relative)
            continue
        visitor = _ExecVisitor(relative)
        visitor.visit(tree)
        sites.extend(visitor.sites)

    lines: list[str] = []
    for site in sites:
        lines.extend(f'{site.location}: {problem}' for problem in site.problems)
        if site.undecidable:
            lines.append(f'{site.location}: could not rule on this call - {site.undecidable}')
    return Evidence(
        lines=lines,
        data={'sites': sites},
        unread=[f'{path}: could not be parsed' for path in unparsed],
    )


def decide_safe_subprocess(evidence: Evidence) -> ItemAssessment:
    """Rule on the gathered call sites."""
    sites: list[_ExecSite] = evidence.data.get('sites', [])
    checklist_id = 'best-practice-safe-subprocess'

    # A file that did not parse is exactly where an unexamined call could be.
    # `ItemCheck.assess` caps the verdict for that, so this only has to rule on
    # what it could read.
    if not sites:
        return ItemAssessment(
            checklist_id=checklist_id,
            verdict=Verdict.NOT_APPLICABLE,
            rationale='The charm does not run any commands in its workload container.',
        )

    failing = [site for site in sites if site.problems]
    if failing:
        return ItemAssessment(
            checklist_id=checklist_id,
            verdict=Verdict.FAIL,
            rationale=(
                f'{len(failing)} of {len(sites)} Container.exec call(s) do not follow '
                f'the best practice.'
            ),
            evidence=evidence.lines,
        )

    undecidable = [site for site in sites if site.undecidable]
    if undecidable:
        return ItemAssessment(
            checklist_id=checklist_id,
            verdict=Verdict.NEEDS_HUMAN,
            rationale=(
                f'{len(undecidable)} of {len(sites)} Container.exec call(s) build their '
                f'command somewhere the check cannot see.'
            ),
            evidence=evidence.lines,
        )

    return ItemAssessment(
        checklist_id=checklist_id,
        verdict=Verdict.PASS,
        rationale=(
            f'All {len(sites)} Container.exec call(s) use absolute paths, pass arguments '
            f'as a list, and wait for the result.'
        ),
    )


safe_subprocess = ItemCheck(
    checklist_id='best-practice-safe-subprocess',
    gather=gather_exec_sites,
    decide=decide_safe_subprocess,
)


# --- ci-automated-releasing ---------------------------------------------------

# Shell commands that publish a charm.
_RELEASE_COMMANDS = (
    re.compile(r'\bcharmcraft\s+upload\b'),
    re.compile(r'\bcharmcraft\s+release\b'),
    re.compile(r'\bcharmcraft\s+upload-resource\b'),
)

# Reusable workflows and actions that publish a charm on the caller's behalf.
# Teams name these both ways round (`charm-release.yaml` in
# canonical/observability, `release-charm.yaml` in charming-actions) and some
# publish the charm as one of several artefacts (`publish-artifacts.yml` in
# canonical/charm-ci), so matching only one spelling misses real releases.
_RELEASE_ACTIONS = (
    re.compile(r'charming-actions/(upload|release)-charm'),
    re.compile(r'/(release|publish|upload)[_-]charm\.ya?ml'),
    re.compile(r'/charm[_-](release|publish|upload)\.ya?ml'),
    re.compile(r'/publish[_-]artifacts?\.ya?ml'),
)

# Channel risk levels that are not `stable`, per the checklist's "unstable
# channels" wording.
_UNSTABLE_CHANNELS = frozenset({'edge', 'beta', 'candidate'})


@dataclasses.dataclass
class _ReleaseWorkflow:
    """A workflow that publishes the charm, and when it runs."""

    path: str
    triggers: str
    default_branch_push: bool
    how: str
    """The step or ``uses:`` that does the publishing."""

    channels: list[str] = dataclasses.field(default_factory=list)
    """Literal channel names found. Empty when they are all expressions."""

    caveats: list[str] = dataclasses.field(default_factory=list)


_CHANNEL_PATTERN = re.compile(r'(?:channel:|--channel[= ]|--release[= ])\s*([A-Za-z0-9._/-]+)')


def _literal_channels(text: str) -> list[str]:
    """Channel names in *text*, ignoring ``${{ }}`` expressions.

    This is only ever given the publishing step's own command, or the ``with:``
    block of the job that calls a publishing action. Scanning the whole
    workflow instead reads a tool install as a release: ``snap install
    charmcraft --channel latest/stable`` is the first line of most release
    workflows, and taking `stable` from it defers the item on a charm that
    releases wherever its `${{ }}` expression points.
    """
    found: list[str] = []
    for match in _CHANNEL_PATTERN.finditer(text):
        value = match.group(1)
        if '${{' in value or not value:
            continue
        # A channel is `track/risk` or just `risk`.
        found.append(value.split('/')[-1])
    return sorted(set(found))


def _job_inputs(workflow: _workflows.Workflow, job_id: str) -> str:
    """The ``with:`` block of one job, as text to read channel names from."""
    job = workflow.jobs.get(job_id)
    if not isinstance(job, dict):
        return ''
    inputs = job.get('with')
    if not isinstance(inputs, dict):
        return ''
    return yaml.safe_dump(inputs, default_flow_style=False)


def gather_release_workflows(source: CharmSource) -> Evidence:
    """Find the workflows that publish the charm, and when they run."""
    workflows = _workflows.load_workflows(source.repo)
    summaries = _workflows.resolve_triggers(workflows, source.default_branch)

    releases: list[_ReleaseWorkflow] = []
    opaque: list[str] = []
    unreadable = [w.path for w in workflows if w.unreadable]
    for workflow in workflows:
        how = ''
        # What to read channel names out of: the publishing command itself, or
        # the inputs of the job that calls a publishing action.
        channel_source = ''
        for location, command in _workflows.step_commands(workflow):
            if _workflows.matches_any(command, _RELEASE_COMMANDS):
                how = f'{location} runs `{command}`'
                channel_source = command
                break
        unrecognised: list[str] = []
        if not how:
            for job_id, uses in _workflows.iter_external_uses(workflow):
                if _workflows.matches_any(uses, _RELEASE_ACTIONS):
                    how = f'{workflow.path} ({job_id}) uses {uses.split("@")[0]}'
                    channel_source = _job_inputs(workflow, job_id)
                    break
                unrecognised.append(f'{workflow.path} ({job_id}) calls {uses.split("@")[0]}')
        if not how:
            # A reusable workflow from another repository may publish the charm
            # without saying so in its name. That is not evidence of absence.
            opaque.extend(unrecognised)
            continue
        summary = summaries[workflow.path]
        releases.append(
            _ReleaseWorkflow(
                path=workflow.path,
                triggers=_workflows.describe_triggers(summary),
                default_branch_push=summary.default_branch_push,
                how=how,
                channels=_literal_channels(channel_source),
                caveats=list(summary.caveats),
            )
        )

    lines = [f'{release.path}: {release.how}; runs on {release.triggers}' for release in releases]
    if not releases:
        lines.extend(f'{entry}, whose contents are in another repository' for entry in opaque)
    return Evidence(
        lines=lines,
        data={
            'releases': releases,
            'opaque': opaque,
            'workflow_count': len(workflows),
        },
        unread=[f'{path}: could not be parsed as YAML' for path in unreadable],
    )


def decide_automated_releasing(evidence: Evidence) -> ItemAssessment:
    """Rule on whether releasing to an unstable channel is automated."""
    checklist_id = 'ci-automated-releasing'
    releases: list[_ReleaseWorkflow] = evidence.data.get('releases', [])
    workflow_count: int = evidence.data.get('workflow_count', 0)

    opaque: list[str] = evidence.data.get('opaque', [])

    if not releases:
        if opaque:
            # Naming a reusable workflow is not reading it: this is undecidable
            # from the repository, not a decided absence.
            return ItemAssessment(
                checklist_id=checklist_id,
                verdict=Verdict.NEEDS_HUMAN,
                rationale=(
                    f'No workflow in this repository publishes the charm, but {len(opaque)} '
                    f'job(s) call reusable workflows in other repositories that might.'
                ),
                evidence=evidence.lines,
            )
        rationale = (
            'No workflow publishes the charm.'
            if workflow_count
            else 'The repository has no GitHub Actions workflows.'
        )
        return ItemAssessment(
            checklist_id=checklist_id,
            verdict=Verdict.FAIL,
            rationale=rationale,
            evidence=evidence.lines,
        )

    automatic = [release for release in releases if release.default_branch_push]
    if not automatic:
        return ItemAssessment(
            checklist_id=checklist_id,
            verdict=Verdict.NEEDS_HUMAN,
            rationale=(
                f'{len(releases)} workflow(s) publish the charm, but none runs on a push to '
                f'the default branch, so releasing is triggered by hand or from elsewhere.'
            ),
            evidence=evidence.lines,
        )

    known = {channel for release in automatic for channel in release.channels}
    if known and not (known & _UNSTABLE_CHANNELS):
        return ItemAssessment(
            checklist_id=checklist_id,
            verdict=Verdict.NEEDS_HUMAN,
            rationale=(
                f'Releasing runs on the default branch, but the only channel(s) named are '
                f'{", ".join(sorted(known))}, which are not unstable channels.'
            ),
            evidence=evidence.lines,
        )

    caveats = [caveat for release in automatic for caveat in release.caveats]
    if caveats:
        return ItemAssessment(
            checklist_id=checklist_id,
            verdict=Verdict.NEEDS_HUMAN,
            rationale=(
                f'Releasing runs on the default branch, but not for every change: {caveats[0]}.'
            ),
            evidence=evidence.lines,
        )

    return ItemAssessment(
        checklist_id=checklist_id,
        verdict=Verdict.PASS,
        rationale=f'{automatic[0].path} publishes the charm on every push to the default branch.',
        evidence=evidence.lines,
    )


automated_releasing = ItemCheck(
    checklist_id='ci-automated-releasing',
    gather=gather_release_workflows,
    decide=decide_automated_releasing,
)


# --- ci-integration-tests -----------------------------------------------------

# Shell commands that run an integration suite.
_INTEGRATION_COMMANDS = (
    re.compile(r'\bcharmcraft\s+test\b'),
    # `\b-e` would never match: there is no word boundary between a space and
    # a hyphen, so the boundary has to go on the other side.
    re.compile(r'\btox\b.*\s-e\s+\S*integration'),
    re.compile(r'\bpytest\b.*integration'),
    re.compile(r'\bmake\s+integration\b'),
    re.compile(r'\bspread\b'),
)

# The checklist excludes tracing endpoints from the coverage requirement.
_EXCLUDED_INTERFACES = frozenset({'tracing'})

# Marks the interpolated part of an f-string, so that `f'{APP}:database'` can
# be told apart from a literal `'other-app:database'`.
_PLACEHOLDER = '\x00'

# The calls that wire two endpoints together. `integrate` is jubilant's and
# modern python-libjuju's; `add_relation` is what pytest-operator suites use
# (`ops_test.model.add_relation`), and is still the more common of the two in
# charms with an established integration suite; `relate` is the older alias.
_INTEGRATE_FUNCTIONS = frozenset({'integrate', 'add_relation', 'relate'})

# Suffixes that distinguish a charm's package name from the application name
# tests deploy it under: `grafana-k8s` is deployed as `grafana`.
_CHARM_NAME_SUFFIXES = ('-k8s', '-operator', '-machine')


def _declared_endpoints(charmcraft: dict[str, Any]) -> tuple[dict[str, str], list[str]]:
    """Return ``{endpoint: role}`` and the endpoints excluded as tracing."""
    endpoints: dict[str, str] = {}
    excluded: list[str] = []
    for role in ('provides', 'requires'):
        block = charmcraft.get(role)
        if not isinstance(block, dict):
            continue
        for name, definition in block.items():
            interface = ''
            if isinstance(definition, dict):
                interface = str(definition.get('interface') or '')
            if interface in _EXCLUDED_INTERFACES:
                excluded.append(f'{name} ({interface})')
                continue
            endpoints[str(name)] = role
    return endpoints, excluded


def _is_integration_test(relative: pathlib.PurePath) -> bool:
    """Is this file part of an integration suite?

    A directory called ``integration`` is the common layout, but it is not the
    only one: a small charm keeps the whole suite in ``tests/test_integration.py``,
    and a spread suite lives under ``tests/spread``. Recognising only the
    directory reports those charms as having no integration tests at all.
    """
    parts = relative.parts
    if 'integration' in parts or 'spread' in parts:
        return True
    return 'integration' in relative.name


def _integration_test_files(charm_path: pathlib.Path) -> list[pathlib.Path]:
    """The Python files that make up the charm's integration suite."""
    return sorted(
        path
        for path in charm_path.rglob('*.py')
        if _is_integration_test(path.relative_to(charm_path))
    )


def _literal_text(node: ast.AST) -> str | None:
    """Render a string literal or f-string, marking interpolated parts."""
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    if isinstance(node, ast.JoinedStr):
        parts: list[str] = []
        for value in node.values:
            if isinstance(value, ast.Constant) and isinstance(value.value, str):
                parts.append(value.value)
            else:
                parts.append(_PLACEHOLDER)
        return ''.join(parts)
    return None


def _names_this_charm(application: str, charm_name: str) -> bool:
    """Is ``application`` the charm under test, on the left of an endpoint?

    Two spellings both mean the charm under test, and both are common:

    * the charm's own name;
    * the charm's name without its packaging suffix - ``grafana-k8s`` is
      deployed as ``grafana``, and its tests write ``'grafana:logging'``.

    Requiring an exact match instead reports a charm with a thorough suite as
    covering almost none of its endpoints.

    An interpolated application (``f'{APP}:database'``) is *not* treated as a
    match. It usually is the charm under test, but ``database``, ``ingress``,
    ``logging`` and ``certificates`` are exactly the endpoint names both
    applications in a test have, so reading it as a match credits this charm
    for integrating somebody else's endpoint. The caller records those calls
    as unattributed instead, which defers the coverage clause rather than
    settling it wrongly in either direction.
    """

    def stem(name: str) -> str:
        name = name.strip().lower()
        for suffix in _CHARM_NAME_SUFFIXES:
            name = name.removesuffix(suffix)
        return name

    return bool(charm_name) and stem(application) == stem(charm_name)


class _IntegrateVisitor(ast.NodeVisitor):
    """Collect the endpoints of this charm that ``integrate()`` calls name."""

    def __init__(self, relative_path: str, charm_name: str, endpoints: set[str]):
        self._path = relative_path
        self._charm_name = charm_name
        self._endpoints = endpoints
        self.covered: dict[str, str] = {}
        """endpoint -> ``path:lineno`` of the call that integrates it."""

        self.unattributed: list[str] = []
        """Calls whose endpoint could not be read."""

    def visit_Call(self, node: ast.Call) -> None:
        func = node.func
        name = func.attr if isinstance(func, ast.Attribute) else getattr(func, 'id', '')
        if name in _INTEGRATE_FUNCTIONS:
            location = f'{self._path}:{getattr(node, "lineno", 0)}'
            for argument in node.args:
                self._record(location, argument)
        self.generic_visit(node)

    def _record(self, location: str, argument: ast.expr) -> None:
        text = _literal_text(argument)
        if text is None:
            self.unattributed.append(f'{location}: endpoint is not a literal')
            return
        if ':' not in text:
            # `integrate('other-app')` names an application, not an endpoint.
            return
        application, endpoint = text.rsplit(':', 1)
        if _PLACEHOLDER in endpoint:
            self.unattributed.append(f'{location}: the endpoint name is interpolated')
            return
        if endpoint not in self._endpoints:
            return
        if _PLACEHOLDER in application:
            self.unattributed.append(f'{location}: the application name is interpolated')
            return
        if not _names_this_charm(application, self._charm_name):
            return
        self.covered.setdefault(endpoint, location)


def gather_integration_tests(source: CharmSource) -> Evidence:
    """Collect the integration suite, its CI triggers, and endpoint coverage."""
    charmcraft = source.charmcraft_yaml()
    charm_name = source.charm_name or str(charmcraft.get('name') or '')
    endpoints, excluded = _declared_endpoints(charmcraft)

    workflows = _workflows.load_workflows(source.repo)
    summaries = _workflows.resolve_triggers(workflows, source.default_branch)
    unreadable = [workflow.path for workflow in workflows if workflow.unreadable]
    running: list[dict[str, Any]] = []
    opaque: list[str] = []
    for workflow in workflows:
        how = ''
        for location, command in _workflows.step_commands(workflow):
            if _workflows.matches_any(command, _INTEGRATION_COMMANDS):
                how = f'{location} runs `{command}`'
                break
        if not how:
            for job_id, uses in _workflows.iter_external_uses(workflow):
                opaque.append(f'{workflow.path} ({job_id}) calls {uses.split("@")[0]}')
            continue
        summary = summaries[workflow.path]
        running.append({
            'path': workflow.path,
            'how': how,
            'triggers': _workflows.describe_triggers(summary),
            'default_branch_push': summary.default_branch_push,
            'caveats': list(summary.caveats),
        })

    test_files = _integration_test_files(source.charm_path)
    covered: dict[str, str] = {}
    unattributed: list[str] = []
    unread_tests: list[str] = []
    for path in test_files:
        relative = path.relative_to(source.charm_path).as_posix()
        try:
            tree = ast.parse(path.read_text(encoding='utf-8', errors='replace'))
        except (SyntaxError, ValueError):
            unread_tests.append(f'{relative}: could not be parsed')
            continue
        visitor = _IntegrateVisitor(relative, charm_name, set(endpoints))
        visitor.visit(tree)
        for endpoint, location in visitor.covered.items():
            covered.setdefault(endpoint, location)
        # Both arguments of one call can be unreadable for the same reason.
        unattributed.extend(line for line in visitor.unattributed if line not in unattributed)

    uncovered = sorted(set(endpoints) - set(covered))

    lines = [f'{entry["path"]}: {entry["how"]}; runs on {entry["triggers"]}' for entry in running]
    lines.extend(
        f'{path.relative_to(source.charm_path).as_posix()}: integration tests'
        for path in test_files
    )
    lines.extend(
        f'{endpoint} ({endpoints[endpoint]}): integrated at {location}'
        for endpoint, location in sorted(covered.items())
    )
    lines.extend(f'{endpoint} ({endpoints[endpoint]}): never integrated' for endpoint in uncovered)
    lines.extend(f'{name}: excluded, tracing' for name in excluded)
    lines.extend(unattributed)
    lines.extend(f'{entry}, whose contents are in another repository' for entry in opaque)

    return Evidence(
        lines=lines,
        unread=([f'{path}: could not be parsed as YAML' for path in unreadable] + unread_tests),
        data={
            'running': running,
            'test_files': [str(path) for path in test_files],
            'endpoints': endpoints,
            'covered': covered,
            'uncovered': uncovered,
            'unattributed': unattributed,
            'opaque': opaque,
        },
    )


def decide_integration_tests(evidence: Evidence) -> ItemAssessment:
    """Rule on the integration suite: does it exist, run, and cover the endpoints?

    This item is a conjunction of three clauses, and they fail independently:
    a charm can have a well-triggered workflow and integrate two of its seven
    endpoints. Each clause is therefore settled on its own and every settled
    failure is reported, rather than returning on the first one - and a clause
    that cannot be settled must not mask one that can. The reviewer gets the
    whole list, not the first thing that went wrong.
    """
    checklist_id = 'ci-integration-tests'
    running: list[dict[str, Any]] = evidence.data.get('running', [])
    test_files: list[str] = evidence.data.get('test_files', [])
    uncovered: list[str] = evidence.data.get('uncovered', [])
    endpoints: dict[str, str] = evidence.data.get('endpoints', {})
    opaque: list[str] = evidence.data.get('opaque', [])

    if not test_files:
        # Without a suite there is nothing for the other two clauses to be
        # about, so this one short-circuits where the others do not - but a
        # workflow running an integration command is evidence that a suite
        # exists in a layout the file walk does not recognise, and the two
        # halves disagreeing is not grounds for saying there is nothing there.
        if running:
            return ItemAssessment(
                checklist_id=checklist_id,
                verdict=Verdict.NEEDS_HUMAN,
                rationale=(
                    f'{running[0]["path"]} runs an integration suite, but no integration '
                    f'tests were found in this charm, so the suite is somewhere this check '
                    f'does not look.'
                ),
                evidence=evidence.lines,
            )
        return ItemAssessment(
            checklist_id=checklist_id,
            verdict=Verdict.FAIL,
            rationale='The charm has no integration tests.',
            evidence=evidence.lines,
        )

    failures: list[str] = []
    deferred: list[str] = []

    automatic = [entry for entry in running if entry['default_branch_push']]
    if running and not automatic:
        failures.append(
            'they are not run on changes to the default branch '
            f'({running[0]["path"]} runs on {running[0]["triggers"]})'
        )
    elif not running and opaque:
        deferred.append(
            f'no workflow in this repository runs them, but {len(opaque)} job(s) call '
            f'reusable workflows in other repositories that might'
        )
    elif not running:
        failures.append('no workflow runs them')

    caveats = [caveat for entry in automatic for caveat in entry['caveats']]
    if caveats:
        failures.append(f'they do not run on every change - {caveats[0]}')

    unattributed: list[str] = evidence.data.get('unattributed', [])
    if uncovered and not unattributed:
        failures.append(
            f'{len(uncovered)} of {len(endpoints)} endpoint(s) are never integrated '
            f'({", ".join(uncovered)})'
        )
    elif uncovered:
        # An endpoint is only *never* integrated if every call that could have
        # integrated it was readable. A suite that builds endpoint names at
        # runtime may well cover them, and reporting a failure on the strength
        # of what could not be read is how a review tool loses its reviewer.
        deferred.append(
            f'{len(uncovered)} of {len(endpoints)} endpoint(s) were not seen integrated '
            f'({", ".join(uncovered)}), but {len(unattributed)} call(s) name their endpoint '
            f'in a way the check cannot read'
        )

    if failures:
        return ItemAssessment(
            checklist_id=checklist_id,
            verdict=Verdict.FAIL,
            rationale=f'The charm has integration tests, but {"; and ".join(failures)}.',
            evidence=evidence.lines,
        )

    # Nothing settled failed. What is left needs either a network call (are the
    # runs green?) or a reading of each test (does integrating an endpoint
    # actually exercise it?).
    deferred.append(
        'whether the runs are passing, and whether each test exercises the endpoint it '
        'integrates rather than only declaring it, is not visible in the source'
    )
    if not endpoints:
        coverage = 'and it declares no endpoints to cover'
    elif uncovered:
        coverage = f'integrating {len(endpoints) - len(uncovered)} of {len(endpoints)} endpoint(s)'
    else:
        coverage = f'covering all {len(endpoints)} endpoint(s)'
    return ItemAssessment(
        checklist_id=checklist_id,
        verdict=Verdict.NEEDS_HUMAN,
        rationale=f'The charm has integration tests {coverage}; {"; and ".join(deferred)}.',
        evidence=evidence.lines,
    )


integration_tests = ItemCheck(
    checklist_id='ci-integration-tests',
    gather=gather_integration_tests,
    decide=decide_integration_tests,
)


# --- best-practice-no-duplicate-model-config ----------------------------------

# Model configuration keys, from
# https://canonical.com/juju/docs/juju-cli/3.6/reference/configuration/list-of-model-configuration-keys
# read 2026-07-28 against Juju 3.6. Juju adds keys occasionally; a key missing
# from this list makes the check miss a duplicate, never invent one.
MODEL_CONFIG_KEYS = frozenset({
    'agent-metadata-url',
    'agent-stream',
    'agent-version',
    'apt-ftp-proxy',
    'apt-http-proxy',
    'apt-https-proxy',
    'apt-mirror',
    'apt-no-proxy',
    'automatically-retry-hooks',
    'backup-dir',
    'charmhub-url',
    'cloudinit-userdata',
    'container-image-metadata-defaults-disabled',
    'container-image-metadata-url',
    'container-image-stream',
    'container-inherit-properties',
    'container-networking-method',
    'default-base',
    'default-space',
    'development',
    'disable-network-management',
    'disable-telemetry',
    'egress-subnets',
    'enable-os-refresh-update',
    'enable-os-upgrade',
    'fan-config',
    'firewall-mode',
    'ftp-proxy',
    'http-proxy',
    'https-proxy',
    'ignore-machine-addresses',
    'image-metadata-defaults-disabled',
    'image-metadata-url',
    'image-stream',
    'juju-ftp-proxy',
    'juju-http-proxy',
    'juju-https-proxy',
    'juju-no-proxy',
    'logforward-enabled',
    'logging-config',
    'logging-output',
    'lxd-snap-channel',
    'max-action-results-age',
    'max-action-results-size',
    'max-status-history-age',
    'max-status-history-size',
    'net-bond-reconfigure-delay',
    'no-proxy',
    'num-container-provision-workers',
    'num-provision-workers',
    'provisioner-harvest-mode',
    'proxy-ssh',
    'resource-tags',
    'secret-backend',
    'snap-http-proxy',
    'snap-https-proxy',
    'snap-store-assertions',
    'snap-store-proxy',
    'snap-store-proxy-url',
    'ssl-hostname-verification',
    'storage-default-block-source',
    'storage-default-filesystem-source',
    'transmit-vendor-metrics',
    'update-status-hook-interval',
})


def _normalise_option(name: str) -> str:
    """Charm options may use either separator, so compare without one."""
    return name.strip().lower().replace('_', '-')


def gather_config_options(source: CharmSource) -> Evidence:
    """Collect the charm's config options and any model-config key they match."""
    charmcraft = source.charmcraft_yaml()
    config = charmcraft.get('config')
    options = config.get('options') if isinstance(config, dict) else None
    if not isinstance(options, dict):
        options = {}

    duplicates: list[tuple[str, str]] = []
    others: list[str] = []
    for name, definition in options.items():
        normalised = _normalise_option(str(name))
        if normalised in MODEL_CONFIG_KEYS:
            duplicates.append((str(name), normalised))
            continue
        description = ''
        if isinstance(definition, dict):
            description = ' '.join(str(definition.get('description') or '').split())
        others.append(f'{name}: {description}' if description else str(name))

    lines = [
        f'{name}: duplicates the `{key}` model configuration key'
        for name, key in sorted(duplicates)
    ]
    return Evidence(
        lines=lines or others,
        data={'duplicates': duplicates, 'others': others, 'option_count': len(options)},
    )


def decide_no_duplicate_model_config(evidence: Evidence) -> ItemAssessment:
    """Rule on whether the charm re-exposes model-level configuration."""
    checklist_id = 'best-practice-no-duplicate-model-config'
    duplicates: list[tuple[str, str]] = evidence.data.get('duplicates', [])
    others: list[str] = evidence.data.get('others', [])

    if not evidence.data.get('option_count'):
        return ItemAssessment(
            checklist_id=checklist_id,
            verdict=Verdict.NOT_APPLICABLE,
            rationale='The charm defines no configuration options.',
        )

    if duplicates:
        names = ', '.join(name for name, _ in sorted(duplicates))
        return ItemAssessment(
            checklist_id=checklist_id,
            verdict=Verdict.FAIL,
            rationale=(
                f'{len(duplicates)} configuration option(s) duplicate a `juju model-config` '
                f'key: {names}.'
            ),
            evidence=evidence.lines,
        )

    # Name matching settles the duplicate-by-name case exactly, and that is the
    # common one. What it cannot settle is an option that controls the same
    # thing under a different name, which needs the descriptions read - so the
    # options and their descriptions are what this hands on.
    return ItemAssessment(
        checklist_id=checklist_id,
        verdict=Verdict.NEEDS_HUMAN,
        rationale=(
            f'None of the {len(others)} configuration option(s) share a name with a '
            f'`juju model-config` key; whether any duplicates one under a different name '
            f'needs their descriptions read.'
        ),
        evidence=evidence.lines,
    )


no_duplicate_model_config = ItemCheck(
    checklist_id='best-practice-no-duplicate-model-config',
    gather=gather_config_options,
    decide=decide_no_duplicate_model_config,
)


# --- best-practice-automated-dependency-updates --------------------------------

# https://docs.renovatebot.com/configuration-options/#locations-for-configuration-filenames
# read 2026-07-29. `package.json`'s `renovate` section is handled separately
# below, since it needs the file parsed rather than merely existing.
_DEPENDABOT_PATHS = ('.github/dependabot.yml', '.github/dependabot.yaml')

_RENOVATE_PATHS = (
    'renovate.json',
    'renovate.jsonc',
    'renovate.json5',
    '.github/renovate.json',
    '.github/renovate.jsonc',
    '.github/renovate.json5',
    '.gitlab/renovate.json',
    '.gitlab/renovate.jsonc',
    '.gitlab/renovate.json5',
    '.renovaterc',
    '.renovaterc.json',
    '.renovaterc.jsonc',
    '.renovaterc.json5',
)


def gather_dependency_update_tooling(source: CharmSource) -> Evidence:
    """Look for a Dependabot or Renovate configuration at every valid location."""
    repo = source.repo
    found: list[str] = [
        relative
        for relative in (*_DEPENDABOT_PATHS, *_RENOVATE_PATHS)
        if (repo / relative).is_file()
    ]

    package_json = repo / 'package.json'
    if package_json.is_file():
        try:
            data = json.loads(package_json.read_text(encoding='utf-8', errors='replace'))
        except json.JSONDecodeError:
            data = {}
        if isinstance(data, dict) and 'renovate' in data:
            found.append('package.json (renovate section)')

    lines = [f'{path}: present' for path in found]
    return Evidence(lines=lines, data={'found': found})


def decide_dependency_update_tooling(evidence: Evidence) -> ItemAssessment:
    """Rule on whether dependency-update tooling is configured."""
    checklist_id = 'best-practice-automated-dependency-updates'
    found: list[str] = evidence.data.get('found', [])

    if found:
        return ItemAssessment(
            checklist_id=checklist_id,
            verdict=Verdict.PASS,
            rationale=f'Dependency-update tooling is configured: {found[0]}.',
            evidence=evidence.lines,
        )
    return ItemAssessment(
        checklist_id=checklist_id,
        verdict=Verdict.FAIL,
        rationale=(
            'No Dependabot or Renovate configuration was found at any location either tool '
            'recognises.'
        ),
    )


dependency_update_tooling = ItemCheck(
    checklist_id='best-practice-automated-dependency-updates',
    gather=gather_dependency_update_tooling,
    decide=decide_dependency_update_tooling,
)


# --- best-practice-avoid-charm-plugin -------------------------------------------

# The two plugins the best practice recommends migrating to. `charm` itself is
# not listed here - it is the thing being avoided, and is checked for by name.
_MIGRATED_PLUGINS = frozenset({'uv', 'poetry'})


def gather_charm_plugin(source: CharmSource) -> Evidence:
    """Collect the plugin each of charmcraft.yaml's parts resolves to.

    Reuses ``evaluate.effective_plugin`` (originally written for
    ``charm_has_icon``): charmcraft infers a part's plugin from its own name
    when no `plugin` key is given, and defaults the whole build to the `charm`
    plugin when `parts` is absent entirely - two different things, so they are
    kept as two different evidence shapes rather than one collapsed to the
    other.
    """
    charmcraft = source.charmcraft_yaml()
    parts = charmcraft.get('parts')
    if not isinstance(parts, dict) or not parts:
        return Evidence(
            lines=['No `parts` are declared, so Charmcraft defaults to the `charm` plugin.'],
            data={'declared': False, 'charm': [], 'migrated': [], 'other': {}},
        )

    charm_parts: list[str] = []
    migrated_parts: list[tuple[str, str]] = []
    other_parts: dict[str, str] = {}
    for name, part in parts.items():
        if not isinstance(part, dict):
            continue
        plugin = effective_plugin(part, str(name))
        if plugin == 'charm':
            charm_parts.append(str(name))
        elif plugin in _MIGRATED_PLUGINS:
            migrated_parts.append((str(name), plugin))
        else:
            other_parts[str(name)] = plugin

    lines = [f'{name}: uses the `charm` plugin' for name in sorted(charm_parts)]
    lines.extend(f'{name}: uses the `{plugin}` plugin' for name, plugin in sorted(migrated_parts))
    lines.extend(
        f'{name}: uses the `{plugin}` plugin' for name, plugin in sorted(other_parts.items())
    )
    return Evidence(
        lines=lines,
        data={
            'declared': True,
            'charm': charm_parts,
            'migrated': migrated_parts,
            'other': other_parts,
        },
    )


def decide_charm_plugin(evidence: Evidence) -> ItemAssessment:
    """Rule on whether the charm still uses Charmcraft's `charm` plugin."""
    checklist_id = 'best-practice-avoid-charm-plugin'
    declared: bool = evidence.data.get('declared', False)
    charm_parts: list[str] = evidence.data.get('charm', [])
    migrated_parts: list[tuple[str, str]] = evidence.data.get('migrated', [])
    other_parts: dict[str, str] = evidence.data.get('other', {})

    if not declared:
        return ItemAssessment(
            checklist_id=checklist_id,
            verdict=Verdict.FAIL,
            rationale=(
                'No `parts` are declared in charmcraft.yaml, so Charmcraft builds the charm '
                'with its default `charm` plugin.'
            ),
            evidence=evidence.lines,
        )

    # A part using `charm` is a settled failure regardless of what any other
    # part resolves to - even a charm mid-migration, with one part already
    # moved to `uv`/`poetry`, has not yet avoided the plugin everywhere.
    if charm_parts:
        return ItemAssessment(
            checklist_id=checklist_id,
            verdict=Verdict.FAIL,
            rationale=(
                f'{len(charm_parts)} part(s) use the `charm` plugin: '
                f'{", ".join(sorted(charm_parts))}.'
            ),
            evidence=evidence.lines,
        )

    if migrated_parts:
        names = ', '.join(f'{name} ({plugin})' for name, plugin in sorted(migrated_parts))
        return ItemAssessment(
            checklist_id=checklist_id,
            verdict=Verdict.PASS,
            rationale=f'The charm is built with a recommended plugin: {names}.',
            evidence=evidence.lines,
        )

    if other_parts:
        names = ', '.join(f'{name} ({plugin})' for name, plugin in sorted(other_parts.items()))
        rationale = (
            f'No part uses the `charm` plugin, but none names `uv` or `poetry` either: {names}.'
        )
    else:
        rationale = '`parts` is declared, but no part could be read as a mapping.'
    return ItemAssessment(
        checklist_id=checklist_id,
        verdict=Verdict.NEEDS_HUMAN,
        rationale=rationale,
        evidence=evidence.lines,
    )


avoid_charm_plugin = ItemCheck(
    checklist_id='best-practice-avoid-charm-plugin',
    gather=gather_charm_plugin,
    decide=decide_charm_plugin,
)


# --- best-practice-pin-workload-versions ---------------------------------

# The checklist item and its linked "APT packages" howto section are both
# written for a machine charm: install the workload with `apt`/`snap`/`pip`,
# and pin the version rather than taking whatever is newest. That framing
# does not survive contact with a Kubernetes charm, whose workload arrives as
# an OCI image rather than an installed package - see
# AI-ITEM-VALIDATION.md's "Item 16 is framed for machine charms" section.
# This check decides which kind of charm it is looking at, then applies the
# rule that actually applies:
#
# * A charm is Kubernetes when charmcraft.yaml declares a non-empty
#   `containers` block - the key that makes Juju run it as an operator pod
#   against a workload container, and, per Charmcraft's own reference, the
#   reason an `oci-image` resource exists at all ("Kubernetes charms must
#   declare an oci-image resource for each container they define"). This is
#   the cause; an oci-image resource on its own is only ever a symptom of it,
#   so `containers` is what this check keys on, not `resources`.
# * Everything else is treated as a machine charm and judged by its install
#   logic, the item as written.
#
# For the Kubernetes branch, `upstream-source` is not a documented
# charmcraft.yaml key - Charmcraft's own resources reference stops at `type`,
# `description` and `filename` - but it is a real, load-bearing convention:
# release tooling in charms like grafana-k8s reads
# `resources.<name>.upstream-source` with `yq` to know what to upload
# (`charms.just`), and Renovate bumps the digest via a trailing `# renovate:`
# comment. Where it is set, this check draws its pinned/floating line
# explicitly, as a deliberate judgement call rather than one fallen into:
# a `@sha256:` digest is pinned; a specific version tag (anything other than
# `latest` or no tag at all) is treated as pinned too, since it is a
# deliberate versioned choice rather than "whatever is newest" - the
# opposite of what the item is guarding against - even though a tag is
# mutable in a way a digest is not, which the rationale says outright rather
# than papering over. Where `upstream-source` is absent, the check has no
# in-repo evidence either way - the real pin happens wherever
# `charmcraft upload-resource --image=...` is run, which for a charm like
# forgejo-k8s is an opaque, external release pipeline - so the verdict is
# NEEDS_HUMAN, not a guessed FAIL.


@dataclasses.dataclass
class _OciResource:
    """One `oci-image` resource, and what charmcraft.yaml says about its pin."""

    name: str
    reference: str
    pin: str
    """`'digest'`, `'tag'`, `'floating'`, or `'unknown'` (no `upstream-source`)."""


def _classify_reference(reference: str) -> str:
    """Where an OCI image reference sits between pinned and floating."""
    if '@sha256:' in reference or '@sha512:' in reference:
        return 'digest'
    # A registry host may itself contain a colon (a port, `host:5000/image`),
    # so only look for a tag after the final slash.
    name = reference.rsplit('/', 1)[-1]
    if ':' not in name:
        return 'floating'  # bare reference - implicitly `latest`
    tag = name.split(':', 1)[1]
    return 'floating' if not tag or tag == 'latest' else 'tag'


def _oci_image_resources(resources: dict[str, Any]) -> list[_OciResource]:
    found: list[_OciResource] = []
    for name, definition in resources.items():
        if not isinstance(definition, dict) or definition.get('type') != 'oci-image':
            continue
        reference = definition.get('upstream-source')
        if isinstance(reference, str) and reference:
            found.append(
                _OciResource(
                    name=str(name), reference=reference, pin=_classify_reference(reference)
                )
            )
        else:
            found.append(_OciResource(name=str(name), reference='', pin='unknown'))
    return found


# Package managers and downloaders whose CLI form this check recognises. The
# Python-library forms (`apt.add_package`, `snap.add`, a `SnapCache` entry's
# `.ensure()`) are matched separately in `_InstallVisitor`, since a charm can
# use either shape - `AI-ITEM-VALIDATION.md`'s real charms use only the
# library forms, but the checklist item's own wording ("snap install
# --channel=stable", "apt install foo") is CLI-shaped.
_CLI_INSTALL_PROGRAMS = frozenset({'apt', 'apt-get', 'snap', 'pip', 'pip3'})
_CLI_DOWNLOAD_PROGRAMS = frozenset({'curl', 'wget'})

# apt.add_package's sibling for a local .deb is not covered: it installs a
# file already on disk, not "the latest available package", so the pinning
# question does not apply to it the way it does to the two below.
_APT_LIB_FUNCTIONS = frozenset({'add_package'})


@dataclasses.dataclass
class _InstallSite:
    """One place the charm installs a workload package."""

    location: str
    pinned: bool | None
    """True=pinned, False=floating, None=the check cannot tell statically."""

    detail: str


def _literal_tokens(node: ast.expr | None) -> list[str] | None:
    """A command's argv as a list of strings, only if every element is literal."""
    if isinstance(node, (ast.List, ast.Tuple)):
        tokens: list[str] = []
        for element in node.elts:
            text = _literal_text(element)
            if text is None or _PLACEHOLDER in text:
                return None
            tokens.append(text)
        return tokens
    if node is None:
        return None
    text = _literal_text(node)
    return text.split() if text is not None and _PLACEHOLDER not in text else None


def _first_program(node: ast.expr | None) -> str | None:
    """The program name of a command, read from just the first argv element.

    Resolved separately from the full argv so that a call whose *program* is
    a literal but whose *arguments* are built elsewhere - a charm that always
    runs `apt-get install` but assembles the package list at runtime - is
    still recognised as an install site, rather than silently skipped the way
    a call this check cannot identify at all is.
    """
    text: str | None
    if isinstance(node, (ast.List, ast.Tuple)) and node.elts:
        text = _literal_text(node.elts[0])
    elif node is None:
        text = None
    else:
        text = _literal_text(node)
        text = text.split()[0] if text and ' ' in text else text
    return pathlib.PurePosixPath(text).name if text else None


class _InstallVisitor(ast.NodeVisitor):
    """Collect the charm's package-install call sites and judge their pinning."""

    def __init__(self, relative_path: str):
        self._path = relative_path
        self.sites: list[_InstallSite] = []

    def _location(self, node: ast.AST) -> str:
        return f'{self._path}:{getattr(node, "lineno", 0)}'

    def visit_Call(self, node: ast.Call) -> None:
        func = node.func
        if isinstance(func, ast.Attribute):
            if _is_name(func.value, 'apt') and func.attr in _APT_LIB_FUNCTIONS:
                self._add_apt_lib_site(node)
            elif _is_name(func.value, 'snap') and func.attr in ('add', 'ensure'):
                self._add_snap_site(node, f'snap.{func.attr}')
            elif func.attr == 'ensure' and {kw.arg for kw in node.keywords} & {
                'channel',
                'revision',
            }:
                # The receiver's name varies too much to match on (a `Snap` from
                # `SnapCache()[name]`, a local, `self._snap`, ...), the same
                # problem `container.exec()` has in the safe-subprocess check.
                # Requiring a `channel`/`revision` keyword is the guard that
                # keeps an unrelated `.ensure()` method from matching.
                self._add_snap_site(node, '.ensure()')
            elif (_is_name(func.value, 'subprocess') and func.attr in _SUBPROCESS_FUNCTIONS) or (
                _is_name(func.value, 'os') and func.attr == 'system'
            ):
                self._add_cli_site(node, node.args[0] if node.args else None)
        self.generic_visit(node)

    def _add_apt_lib_site(self, node: ast.Call) -> None:
        keywords = {kw.arg: kw.value for kw in node.keywords if kw.arg}
        version = keywords.get('version')
        if version is None:
            self.sites.append(
                _InstallSite(
                    location=self._location(node),
                    pinned=False,
                    detail=(
                        'apt.add_package with no `version`, so it installs whatever apt '
                        'currently resolves as the candidate'
                    ),
                )
            )
            return
        literal = _literal_text(version)
        if literal is not None and _PLACEHOLDER not in literal:
            self.sites.append(
                _InstallSite(
                    location=self._location(node),
                    pinned=True,
                    detail=f'apt.add_package pinned to version {literal!r}',
                )
            )
            return
        # `version=` is set, but computed rather than a literal - which covers
        # both a real pin read from elsewhere and operator-libs-linux's own
        # "install whatever apt-cache's candidate is" idiom
        # (github.com/canonical/operator-libs-linux/issues/113). Both look
        # identical at this call site, so this is genuinely undecidable
        # statically, not a confident pin.
        self.sites.append(
            _InstallSite(
                location=self._location(node),
                pinned=None,
                detail=(
                    'apt.add_package passes a `version` computed elsewhere, which this check '
                    'cannot resolve statically'
                ),
            )
        )

    def _add_snap_site(self, node: ast.Call, how: str) -> None:
        keywords = {kw.arg: kw.value for kw in node.keywords if kw.arg}
        if 'revision' in keywords:
            # Unlike apt's `version`, a snap `revision` is an immutable
            # per-build identifier by construction - there is no equivalent of
            # apt-cache's "candidate" idiom in common use, so a computed
            # revision is treated as pinned rather than deferred. A contrived
            # "whatever revision this channel currently has" helper would defeat
            # this; none of the real charms this check was validated against do.
            literal = _literal_text(keywords['revision'])
            detail = (
                f'{how} pinned to revision {literal!r}'
                if literal
                else f'{how} pinned to a `revision`'
            )
            self.sites.append(
                _InstallSite(location=self._location(node), pinned=True, detail=detail)
            )
            return
        if 'channel' in keywords:
            literal = _literal_text(keywords['channel'])
            label = f' ({literal!r})' if literal else ''
            self.sites.append(
                _InstallSite(
                    location=self._location(node),
                    pinned=False,
                    detail=(
                        f'{how} installs by channel{label}, which tracks whatever is newest '
                        f'in that channel'
                    ),
                )
            )
            return
        self.sites.append(
            _InstallSite(
                location=self._location(node),
                pinned=False,
                detail=(
                    f'{how} gives no channel or revision, so it installs the default '
                    f'(newest) release'
                ),
            )
        )

    def _add_cli_site(self, node: ast.Call, argv: ast.expr | None) -> None:
        program = _first_program(argv)
        if program not in _CLI_INSTALL_PROGRAMS and program not in _CLI_DOWNLOAD_PROGRAMS:
            return
        tokens = _literal_tokens(argv)
        if tokens is None:
            self.sites.append(
                _InstallSite(
                    location=self._location(node),
                    pinned=None,
                    detail=(
                        f'runs {program}, but its arguments are built elsewhere so the check '
                        f'cannot read what it installs'
                    ),
                )
            )
            return
        if program in ('apt', 'apt-get'):
            self._judge_package_list(node, tokens, program, separator='=')
        elif program in ('pip', 'pip3'):
            self._judge_package_list(node, tokens, program, separator='==')
        elif program == 'snap':
            self._judge_snap_cli(node, tokens)
        else:
            self._judge_download(node, tokens, program)

    def _judge_package_list(
        self, node: ast.Call, tokens: list[str], program: str, *, separator: str
    ) -> None:
        if 'install' not in tokens:
            return
        packages = [t for t in tokens[tokens.index('install') + 1 :] if not t.startswith('-')]
        if not packages:
            return
        unpinned = [p for p in packages if separator not in p]
        if unpinned:
            self.sites.append(
                _InstallSite(
                    location=self._location(node),
                    pinned=False,
                    detail=(
                        f'{program} install {", ".join(unpinned)} with no `{separator}` '
                        f'version pin'
                    ),
                )
            )
        else:
            self.sites.append(
                _InstallSite(
                    location=self._location(node),
                    pinned=True,
                    detail=f'{program} install pinned via `{separator}`',
                )
            )

    def _judge_snap_cli(self, node: ast.Call, tokens: list[str]) -> None:
        if 'install' not in tokens:
            return
        if any(t.startswith('--revision') for t in tokens):
            self.sites.append(
                _InstallSite(
                    location=self._location(node),
                    pinned=True,
                    detail='snap install pinned via --revision',
                )
            )
            return
        channel = next((t for t in tokens if t.startswith('--channel')), None)
        detail = (
            f'snap install by channel ({channel})'
            if channel
            else (
                'snap install with no --channel or --revision, so it tracks the default '
                '(stable) channel'
            )
        )
        self.sites.append(_InstallSite(location=self._location(node), pinned=False, detail=detail))

    def _judge_download(self, node: ast.Call, tokens: list[str], program: str) -> None:
        urls = [t for t in tokens[1:] if not t.startswith('-')]
        if not urls:
            return
        url = urls[0]
        if 'latest' in pathlib.PurePosixPath(url).as_posix().lower():
            self.sites.append(
                _InstallSite(
                    location=self._location(node),
                    pinned=False,
                    detail=f"{program} downloads {url!r}, which names 'latest'",
                )
            )
            return
        self.sites.append(
            _InstallSite(
                location=self._location(node),
                pinned=None,
                detail=(
                    f'{program} downloads {url!r}; whether the URL is pinned to a version '
                    f'needs a human to check'
                ),
            )
        )


def gather_pin_workload_versions(source: CharmSource) -> Evidence:
    """Collect the evidence for whichever branch this charm's kind needs."""
    charmcraft = source.charmcraft_yaml()
    containers = charmcraft.get('containers')

    if isinstance(containers, dict) and containers:
        resources = charmcraft.get('resources')
        oci_resources = _oci_image_resources(resources) if isinstance(resources, dict) else []
        lines = [
            f'{r.name}: {r.pin} ({r.reference})'
            if r.reference
            else f'{r.name}: no `upstream-source` given'
            for r in oci_resources
        ]
        return Evidence(lines=lines, data={'kind': 'kubernetes', 'oci_resources': oci_resources})

    sites: list[_InstallSite] = []
    unparsed: list[str] = []
    for path in first_party_python_files(source.charm_path, source.charm_name):
        relative = path.relative_to(source.charm_path).as_posix()
        try:
            tree = ast.parse(path.read_text(encoding='utf-8', errors='replace'))
        except (SyntaxError, ValueError):
            unparsed.append(relative)
            continue
        visitor = _InstallVisitor(relative)
        visitor.visit(tree)
        sites.extend(visitor.sites)

    lines = [f'{site.location}: {site.detail}' for site in sites]
    lines.extend(f'{path}: could not be parsed' for path in unparsed)
    return Evidence(lines=lines, data={'kind': 'machine', 'sites': sites, 'unparsed': unparsed})


def decide_pin_workload_versions(evidence: Evidence) -> ItemAssessment:
    """Rule on whether the workload's version is pinned, machine or Kubernetes."""
    checklist_id = 'best-practice-pin-workload-versions'
    if evidence.data.get('kind') == 'kubernetes':
        return _decide_oci_pinning(checklist_id, evidence)
    return _decide_install_pinning(checklist_id, evidence)


def _decide_oci_pinning(checklist_id: str, evidence: Evidence) -> ItemAssessment:
    resources: list[_OciResource] = evidence.data.get('oci_resources', [])
    if not resources:
        return ItemAssessment(
            checklist_id=checklist_id,
            verdict=Verdict.NOT_APPLICABLE,
            rationale='The charm declares containers but no `oci-image` resource for them.',
        )

    floating = [r for r in resources if r.pin == 'floating']
    if floating:
        names = ', '.join(sorted(r.name for r in floating))
        return ItemAssessment(
            checklist_id=checklist_id,
            verdict=Verdict.FAIL,
            rationale=(
                f'{len(floating)} of {len(resources)} OCI resource(s) float to `latest` or an '
                f'untagged reference: {names}.'
            ),
            evidence=evidence.lines,
        )

    unknown = [r for r in resources if r.pin == 'unknown']
    if unknown:
        names = ', '.join(sorted(r.name for r in unknown))
        return ItemAssessment(
            checklist_id=checklist_id,
            verdict=Verdict.NEEDS_HUMAN,
            rationale=(
                f'{len(unknown)} of {len(resources)} OCI resource(s) give no `upstream-source` in '
                f'charmcraft.yaml: {names}. The reference actually published is set wherever '
                f'`charmcraft upload-resource --image=...` runs, which is not always in this '
                f"repository - check the resource's published revisions on Charmhub."
            ),
            evidence=evidence.lines,
        )

    digests = [r.name for r in resources if r.pin == 'digest']
    tags = [r.name for r in resources if r.pin == 'tag']
    detail = []
    if digests:
        detail.append(f'{len(digests)} pinned to a digest')
    if tags:
        detail.append(
            f'{len(tags)} pinned to a version tag (not a digest, but not floating either)'
        )
    return ItemAssessment(
        checklist_id=checklist_id,
        verdict=Verdict.PASS,
        rationale=f'All {len(resources)} OCI resource(s) are pinned: {", ".join(detail)}.',
        evidence=evidence.lines,
    )


def _decide_install_pinning(checklist_id: str, evidence: Evidence) -> ItemAssessment:
    sites: list[_InstallSite] = evidence.data.get('sites', [])
    unparsed: list[str] = evidence.data.get('unparsed', [])

    if not sites:
        if unparsed:
            return ItemAssessment(
                checklist_id=checklist_id,
                verdict=Verdict.NEEDS_HUMAN,
                rationale=(
                    f'Found no package-install logic, but {len(unparsed)} file(s) could not be '
                    f'parsed.'
                ),
                evidence=[f'{path}: could not be parsed' for path in unparsed],
            )
        return ItemAssessment(
            checklist_id=checklist_id,
            verdict=Verdict.NOT_APPLICABLE,
            rationale='The charm installs no package or workload from its own source.',
        )

    unpinned = [site for site in sites if site.pinned is False]
    if unpinned:
        return ItemAssessment(
            checklist_id=checklist_id,
            verdict=Verdict.FAIL,
            rationale=(
                f'{len(unpinned)} of {len(sites)} install call(s) do not pin a version '
                f'or revision.'
            ),
            evidence=evidence.lines,
        )

    undecidable = [site for site in sites if site.pinned is None]
    if undecidable:
        return ItemAssessment(
            checklist_id=checklist_id,
            verdict=Verdict.NEEDS_HUMAN,
            rationale=(
                f'{len(undecidable)} of {len(sites)} install call(s) compute their version '
                f'somewhere this check cannot see.'
            ),
            evidence=evidence.lines,
        )

    return ItemAssessment(
        checklist_id=checklist_id,
        verdict=Verdict.PASS,
        rationale=f'All {len(sites)} install call(s) pin the version or revision they install.',
        evidence=evidence.lines,
    )


pin_workload_versions = ItemCheck(
    checklist_id='best-practice-pin-workload-versions',
    gather=gather_pin_workload_versions,
    decide=decide_pin_workload_versions,
)


# --- charmcraft-summary-description -------------------------------------

# `evaluate.py`'s `metadata_links` already gates presence and non-default
# values for `summary` and `description` - that is not this item's to build.
# What is left is the quality comparison between the two, which
# `metadata_links` never makes: does `description` actually say more than
# `summary`, or is it the same sentence reworded? See AI-ITEM-VALIDATION.md's
# "Item 8" section and PLAN.md's design note for the real example this is
# calibrated on (`forgejo-k8s`: summary "Charmed Kubernetes operator for
# Forgejo.", description "Deploy and configure the software forge,
# Forgejo." - barely longer, and not meaningfully more informative).

_WORD_RE = re.compile(r"[a-z0-9']+")

# Below this length ratio, `description` reads as "about the same length as
# summary" rather than "a fuller description" - a real multi-sentence
# description (evaluate.py's own default template runs to four paragraphs)
# clears this by a wide margin. Combined with a minimum vocabulary overlap so
# a short description that happens to share no words with summary (unrelated
# content, not a paraphrase) is not swept in by length alone.
_PARAPHRASE_MAX_LENGTH_RATIO = 2.0
_PARAPHRASE_MIN_OVERLAP = 0.2


def _normalise_words(text: str) -> list[str]:
    return _WORD_RE.findall(text.lower())


def gather_summary_description(source: CharmSource) -> Evidence:
    """Collect the summary and description strings, nothing else."""
    charmcraft = source.charmcraft_yaml()
    summary = str(charmcraft.get('summary') or '')
    description = str(charmcraft.get('description') or '')
    lines = []
    if summary:
        lines.append(f'summary: {summary}')
    if description:
        lines.append(f'description: {description}')
    return Evidence(lines=lines, data={'summary': summary, 'description': description})


def decide_summary_description(evidence: Evidence) -> ItemAssessment:
    """Rule on whether description is a paraphrase of summary.

    One confident branch only: description is not meaningfully longer than
    summary and shares a good share of its vocabulary - a paraphrase, not a
    fuller description. Everything else, including a charm that would
    obviously pass, is left to a human - judging whether a longer
    description is actually *good* (covers configuration, limitations,
    deviations from the unpackaged application) is prose judgement this
    check should not assert a PASS on.
    """
    checklist_id = 'charmcraft-summary-description'
    summary: str = evidence.data.get('summary', '')
    description: str = evidence.data.get('description', '')

    if not summary or not description:
        return ItemAssessment(
            checklist_id=checklist_id,
            verdict=Verdict.NEEDS_HUMAN,
            rationale=(
                'summary and/or description is missing (or metadata_links already flags it as '
                'missing/default) - there is nothing to compare.'
            ),
            evidence=evidence.lines,
        )

    summary_words = _normalise_words(summary)
    description_words = _normalise_words(description)
    if not summary_words:
        return ItemAssessment(
            checklist_id=checklist_id,
            verdict=Verdict.NEEDS_HUMAN,
            rationale='summary has no comparable content to check description against.',
            evidence=evidence.lines,
        )

    length_ratio = len(description_words) / len(summary_words)
    overlap = len(set(summary_words) & set(description_words)) / len(set(summary_words))

    if length_ratio < _PARAPHRASE_MAX_LENGTH_RATIO and overlap >= _PARAPHRASE_MIN_OVERLAP:
        return ItemAssessment(
            checklist_id=checklist_id,
            verdict=Verdict.FAIL,
            rationale=(
                'description is not meaningfully longer than summary and shares a good share '
                'of its vocabulary - it reads as a paraphrase rather than a fuller description.'
            ),
            evidence=evidence.lines,
        )

    return ItemAssessment(
        checklist_id=checklist_id,
        verdict=Verdict.NEEDS_HUMAN,
        rationale=(
            'description is not a simple paraphrase of summary by length and overlap; whether '
            'it is actually a good, detailed description needs a human to judge.'
        ),
        evidence=evidence.lines,
    )


summary_description = ItemCheck(
    checklist_id='charmcraft-summary-description',
    gather=gather_summary_description,
    decide=decide_summary_description,
)


# --- best-practice-use-logging-not-print ---------------------------------

# Upstream text: "avoid `print()` calls, and ensure that any subprocess calls
# capture output." Two independent, differently-shaped clauses:
#
# * print() is a clean, standalone, purely mechanical AST pass - any
#   first-party call to the bare `print` builtin. ruff's T201/T203 answer the
#   same question and should be preferred once the listing-review ruff
#   configuration exists; this stays until it does.
# * "external commands capture their output" is *not* checked here, and is
#   not checked anywhere. It has no ruff rule (nothing in flake8-bandit or
#   pylint covers `capture_output=`), and the AST pass that used to answer it
#   here went with the subprocess half of item 17, which ruff does better.
#   canonical/charmlint#216 proposes it as a rule; until that lands, a charm
#   tipping its workload's stderr into the Juju log passes this item.


class _PrintVisitor(ast.NodeVisitor):
    """Collect first-party calls to the bare ``print`` builtin."""

    def __init__(self, relative_path: str):
        self._path = relative_path
        self.locations: list[str] = []

    def visit_Call(self, node: ast.Call) -> None:
        if isinstance(node.func, ast.Name) and node.func.id == 'print':
            self.locations.append(f'{self._path}:{getattr(node, "lineno", 0)}')
        self.generic_visit(node)


def gather_logging_not_print(source: CharmSource) -> Evidence:
    """Collect the charm's calls to the bare ``print`` builtin."""
    prints: list[str] = []
    unparsed: list[str] = []
    for path in first_party_python_files(source.charm_path, source.charm_name):
        relative = path.relative_to(source.charm_path).as_posix()
        try:
            tree = ast.parse(path.read_text(encoding='utf-8', errors='replace'))
        except (SyntaxError, ValueError):
            unparsed.append(relative)
            continue
        visitor = _PrintVisitor(relative)
        visitor.visit(tree)
        prints.extend(visitor.locations)

    lines = [f'{location}: print()' for location in prints]
    return Evidence(
        lines=lines,
        data={'prints': prints},
        unread=[f'{path}: could not be parsed' for path in unparsed],
    )


def decide_logging_not_print(evidence: Evidence) -> ItemAssessment:
    """Rule on whether the charm writes to stdout instead of logging."""
    checklist_id = 'best-practice-use-logging-not-print'
    prints: list[str] = evidence.data.get('prints', [])

    if prints:
        return ItemAssessment(
            checklist_id=checklist_id,
            verdict=Verdict.FAIL,
            rationale=f'{len(prints)} call(s) to print() were found.',
            evidence=evidence.lines,
        )

    # A file that would not parse caps this at NEEDS_HUMAN, in
    # `ItemCheck.assess` rather than here.
    return ItemAssessment(
        checklist_id=checklist_id,
        verdict=Verdict.PASS,
        rationale=(
            'No print() calls were found. Whether external commands capture their output '
            'is part of this item and is not checked - see the note above this check.'
        ),
    )


logging_not_print = ItemCheck(
    checklist_id='best-practice-use-logging-not-print',
    gather=gather_logging_not_print,
    decide=decide_logging_not_print,
)


# --- best-practice-no-sensitive-data-in-logs ------------------------------

# AI-ITEM-VALIDATION.md's "Item 12 found a real violation" section is the
# design brief: `forgejo-k8s`'s src/charm.py logged a whole database relation
# databag at logger.debug immediately before reading the password out of it.
# Three shapes are what credentials actually leak through and are greppable:
# a whole relation databag, the charm's own `self.config` mapping, or
# `os.environ`. This is deliberately not a general secret-scanner - logging
# one specific field pulled out of any of those three (`data["endpoint"]`,
# `self.config["some-key"]`) is not caught, and should not be guessed at.

# `logger` is charm code's near-universal convention
# (`logger = logging.getLogger(__name__)`, per operator's own template);
# `logging.<level>` covers a direct call to the root logger. Both are "a
# logging-module-shaped call" per the checklist item's own wording. A
# receiver under any other name is a known, narrow gap, not a guess.
_LOG_LEVELS = frozenset({'debug', 'info', 'warning', 'error', 'exception', 'critical'})


@dataclasses.dataclass
class _LogFinding:
    """One logging call that logs a whole sensitive mapping."""

    location: str
    what: str


class _LoggingVisitor(ast.NodeVisitor):
    """Collect first-party logging calls whose argument is a sensitive shape.

    Resolution follows the same "assigned exactly once to a local" idea
    `_ExecVisitor` uses for command literals - charms routinely fetch data a
    line or two before logging it, same as they build a command before
    running it. The receiver shapes differ enough from command literals that
    a shared helper is not obviously worth it, so this is its own small
    scope-tracking pass rather than a reuse of `_ExecVisitor`'s.
    """

    def __init__(self, relative_path: str):
        self._path = relative_path
        self.findings: list[_LogFinding] = []
        self.call_count = 0
        self._scopes: list[dict[str, ast.expr]] = []

    def visit_Module(self, node: ast.Module) -> None:
        self._visit_scope(node, node.body)

    def visit_FunctionDef(self, node: ast.FunctionDef) -> None:
        self._visit_scope(node, node.body)

    def visit_AsyncFunctionDef(self, node: ast.AsyncFunctionDef) -> None:
        self._visit_scope(node, node.body)

    def _visit_scope(self, node: ast.AST, body: list[ast.stmt]) -> None:
        self._push_scope(body)
        self.generic_visit(node)
        self._scopes.pop()

    def _push_scope(self, body: list[ast.stmt]) -> None:
        assigned: dict[str, list[ast.expr]] = {}
        for statement in ast.walk(ast.Module(body=body, type_ignores=[])):
            if not isinstance(statement, ast.Assign):
                continue
            for target in statement.targets:
                if isinstance(target, ast.Name):
                    assigned.setdefault(target.id, []).append(statement.value)
        self._scopes.append({
            name: values[0] for name, values in assigned.items() if len(values) == 1
        })

    def _resolve(self, node: ast.expr) -> ast.expr:
        if not isinstance(node, ast.Name):
            return node
        for scope in reversed(self._scopes):
            if node.id in scope:
                return scope[node.id]
        return node

    def visit_Call(self, node: ast.Call) -> None:
        func = node.func
        is_logging_call = (
            isinstance(func, ast.Attribute)
            and func.attr in _LOG_LEVELS
            and (_is_name(func.value, 'logger') or _is_name(func.value, 'logging'))
        )
        if is_logging_call:
            self.call_count += 1
            location = f'{self._path}:{getattr(node, "lineno", 0)}'
            seen: set[str] = set()
            for argument in node.args:
                what = _classify_sensitive(self._resolve(argument))
                if what and what not in seen:
                    seen.add(what)
                    self.findings.append(_LogFinding(location=location, what=what))
        self.generic_visit(node)


def _classify_sensitive(node: ast.expr) -> str | None:
    """Which of the three known-sensitive shapes ``node`` is, if any.

    Only the whole-mapping shape is recognised - a subscript that pulls one
    field out (``data["password"]``, ``self.config["key"]``) does not match
    any branch here and is deliberately left alone.
    """
    if isinstance(node, ast.Call):
        func = node.func
        if isinstance(func, ast.Attribute) and func.attr == 'fetch_relation_data':
            return 'the return value of fetch_relation_data()'
        return None
    if isinstance(node, ast.Subscript):
        value = node.value
        if isinstance(value, ast.Attribute) and value.attr == 'data':
            return 'a relation databag (`.data[...]`)'
        return None
    if isinstance(node, ast.Attribute):
        if node.attr == 'config' and _is_name(node.value, 'self'):
            return 'self.config'
        if node.attr == 'environ' and _is_name(node.value, 'os'):
            return 'os.environ'
    return None


def gather_sensitive_logging(source: CharmSource) -> Evidence:
    """Collect first-party logging calls that log a whole sensitive mapping."""
    findings: list[_LogFinding] = []
    unparsed: list[str] = []
    call_count = 0
    for path in first_party_python_files(source.charm_path, source.charm_name):
        relative = path.relative_to(source.charm_path).as_posix()
        try:
            tree = ast.parse(path.read_text(encoding='utf-8', errors='replace'))
        except (SyntaxError, ValueError):
            unparsed.append(relative)
            continue
        visitor = _LoggingVisitor(relative)
        visitor.visit(tree)
        findings.extend(visitor.findings)
        call_count += visitor.call_count

    lines = [f'{finding.location}: logs {finding.what}' for finding in findings]
    lines.extend(f'{path}: could not be parsed' for path in unparsed)
    return Evidence(
        lines=lines,
        data={'findings': findings, 'unparsed': unparsed, 'call_count': call_count},
    )


def decide_sensitive_logging(evidence: Evidence) -> ItemAssessment:
    """Rule on whether a logging call writes a whole sensitive mapping to the log."""
    checklist_id = 'best-practice-no-sensitive-data-in-logs'
    findings: list[_LogFinding] = evidence.data.get('findings', [])
    unparsed: list[str] = evidence.data.get('unparsed', [])
    call_count: int = evidence.data.get('call_count', 0)

    if findings:
        locations = ', '.join(sorted({finding.location for finding in findings}))
        return ItemAssessment(
            checklist_id=checklist_id,
            verdict=Verdict.FAIL,
            rationale=(
                f'{len(findings)} logging call(s) log a whole relation databag, `self.config`, '
                f'or `os.environ` rather than a specific field: {locations}.'
            ),
            evidence=evidence.lines,
        )

    if unparsed:
        # A file that could not be read might contain the very call this
        # item is looking for - "we saw no logging calls" is not a claim
        # this check can make while some of the source went unread.
        return ItemAssessment(
            checklist_id=checklist_id,
            verdict=Verdict.NEEDS_HUMAN,
            rationale=(
                f'No logging call was found to log a whole relation databag, `self.config`, or '
                f'`os.environ`, but {len(unparsed)} file(s) could not be parsed.'
            ),
            evidence=[f'{path}: could not be parsed' for path in unparsed],
        )

    if not call_count:
        return ItemAssessment(
            checklist_id=checklist_id,
            verdict=Verdict.NOT_APPLICABLE,
            rationale='The charm has no logging calls to check.',
        )

    return ItemAssessment(
        checklist_id=checklist_id,
        verdict=Verdict.PASS,
        rationale=(
            f"None of the charm's {call_count} logging call(s) log a whole relation databag, "
            f'`self.config`, or `os.environ`.'
        ),
    )


sensitive_logging = ItemCheck(
    checklist_id='best-practice-no-sensitive-data-in-logs',
    gather=gather_sensitive_logging,
    decide=decide_sensitive_logging,
)


# --- charmcraft-yaml-key-resources -----------------------------------------

# Deliberately thin - see PLAN.md's "Item 23" design note. The only evidence
# that lives in the repository is whether the item applies at all: a
# `type: file` resource declared in `charmcraft.yaml`. Whether that resource
# actually has a revision published for every architecture the charm claims
# to support is a fact that lives entirely on Charmhub - `gather` here only
# ever reads `CharmSource`, so answering that needs `ItemCheck` to grow the
# ability to make a network call, which is a real design change (shared with
# item #2, the Charmhub-page item) to be made explicitly, once, not implied
# by this item's implementation. Until then: NOT_APPLICABLE for no `file`
# resource (a real, free result) and NEEDS_HUMAN otherwise, naming the
# declared resources and the charm's claimed architectures.


def _platform_architectures(charmcraft: dict[str, Any]) -> list[str]:
    """Architectures the charm claims to support, from `platforms:`.

    A platform key is either a bare architecture (``amd64``) or
    ``<base>@<version>:<arch>``; only the part after the last ``:`` is the
    architecture.
    """
    platforms = charmcraft.get('platforms')
    if not isinstance(platforms, dict):
        return []
    return sorted({str(key).rsplit(':', 1)[-1] for key in platforms})


def gather_resource_architectures(source: CharmSource) -> Evidence:
    """Collect declared `type: file` resources and the charm's claimed archs."""
    charmcraft = source.charmcraft_yaml()
    resources = charmcraft.get('resources')
    resources = resources if isinstance(resources, dict) else {}

    file_resources = sorted(
        str(name)
        for name, definition in resources.items()
        if isinstance(definition, dict) and definition.get('type') == 'file'
    )
    architectures = _platform_architectures(charmcraft)

    lines = [f'{name}: `type: file` resource' for name in file_resources]
    lines.extend(f'claims to support: {arch}' for arch in architectures)
    return Evidence(
        lines=lines,
        data={'file_resources': file_resources, 'architectures': architectures},
    )


def decide_resource_architectures(evidence: Evidence) -> ItemAssessment:
    """Rule on binary-resource architecture coverage - as far as it can be judged.

    Whether each declared `file` resource has a published revision for every
    claimed architecture is a Charmhub-only fact this check has no way to
    read; see the module comment above. What is free: whether the item
    applies at all.
    """
    checklist_id = 'charmcraft-yaml-key-resources'
    file_resources: list[str] = evidence.data.get('file_resources', [])
    architectures: list[str] = evidence.data.get('architectures', [])

    if not file_resources:
        return ItemAssessment(
            checklist_id=checklist_id,
            verdict=Verdict.NOT_APPLICABLE,
            rationale='The charm declares no `type: file` resource.',
        )

    names = ', '.join(file_resources)
    if architectures:
        archs = ', '.join(architectures)
        rationale = (
            f'The charm declares {len(file_resources)} `type: file` resource(s) ({names}) and '
            f'claims to support {archs}; whether each has a published revision for every one '
            f'of those architectures can only be checked on Charmhub.'
        )
    else:
        rationale = (
            f'The charm declares {len(file_resources)} `type: file` resource(s) ({names}) but '
            f'names no `platforms`, so the architectures to check on Charmhub are not stated '
            f'here.'
        )
    return ItemAssessment(
        checklist_id=checklist_id,
        verdict=Verdict.NEEDS_HUMAN,
        rationale=rationale,
        evidence=evidence.lines,
    )


resource_architectures = ItemCheck(
    checklist_id='charmcraft-yaml-key-resources',
    gather=gather_resource_architectures,
    decide=decide_resource_architectures,
)


# --- best-practice-libraries-no-status-mutation ---------------------------

# AI-ITEM-VALIDATION.md's "Item 15 needs scoping or it fails every charm"
# section is the whole design: judged against every file under `lib/`, this
# item fails almost any charm on code its author did not write - a charm
# typically vendors far more third-party charmlibs than it publishes its
# own. Scoped to `lib/charms/<this charm's name>/` - the library or
# libraries the repository *publishes* - the same scoping
# `first_party_python_files` already applies everywhere else in this module,
# just intersected with the library-only subtree rather than every
# first-party file. See PLAN.md's design note for the real-charm evidence
# behind both the scoping and the AST pattern below.


@dataclasses.dataclass
class _StatusMutation:
    """One place an own-published library sets unit or application status."""

    location: str
    receiver: str
    """``'unit'`` or ``'app'``."""


class _StatusMutationVisitor(ast.NodeVisitor):
    """Collect assignments to ``<...>.unit.status`` or ``<...>.app.status``.

    Real mutation sites vary in how many attributes come before ``unit``/
    ``app`` - ``self.unit.status``, ``self.model.unit.status``,
    ``self.model.app.status``, ``self.charm.unit.status`` are all real,
    live examples found across this project's vendored charmlibs (see
    PLAN.md). Only the last two attributes of the assignment target matter,
    the same "receiver name varies too much to match on" reasoning
    `_add_container_exec` already uses for ``container.exec``.
    """

    def __init__(self, relative_path: str):
        self._path = relative_path
        self.mutations: list[_StatusMutation] = []

    def _check_targets(self, targets: list[ast.expr], node: ast.AST) -> None:
        for target in targets:
            if (
                isinstance(target, ast.Attribute)
                and target.attr == 'status'
                and isinstance(target.value, ast.Attribute)
                and target.value.attr in ('unit', 'app')
            ):
                self.mutations.append(
                    _StatusMutation(
                        location=f'{self._path}:{getattr(node, "lineno", 0)}',
                        receiver=target.value.attr,
                    )
                )

    def visit_Assign(self, node: ast.Assign) -> None:
        self._check_targets(node.targets, node)
        self.generic_visit(node)

    def visit_AugAssign(self, node: ast.AugAssign) -> None:
        self._check_targets([node.target], node)
        self.generic_visit(node)


def _own_library_files(source: CharmSource) -> list[pathlib.Path]:
    """The Python files under the charm's own published ``lib/charms/`` tree."""
    return [
        path
        for path in first_party_python_files(source.charm_path, source.charm_name)
        if path.relative_to(source.charm_path).parts[:2] == _LIB_CHARMS
    ]


def gather_library_status_mutation(source: CharmSource) -> Evidence:
    """Collect status-mutation sites in the charm's own published library."""
    mutations: list[_StatusMutation] = []
    unparsed: list[str] = []
    files = _own_library_files(source)
    for path in files:
        relative = path.relative_to(source.charm_path).as_posix()
        try:
            tree = ast.parse(path.read_text(encoding='utf-8', errors='replace'))
        except (SyntaxError, ValueError):
            unparsed.append(relative)
            continue
        visitor = _StatusMutationVisitor(relative)
        visitor.visit(tree)
        mutations.extend(visitor.mutations)

    lines = [f'{m.location}: sets .{m.receiver}.status' for m in mutations]
    lines.extend(f'{path}: could not be parsed' for path in unparsed)
    return Evidence(
        lines=lines,
        data={'mutations': mutations, 'unparsed': unparsed, 'own_library_file_count': len(files)},
    )


def decide_library_status_mutation(evidence: Evidence) -> ItemAssessment:
    """Rule on whether the charm's own library mutates unit/app status."""
    checklist_id = 'best-practice-libraries-no-status-mutation'
    mutations: list[_StatusMutation] = evidence.data.get('mutations', [])
    unparsed: list[str] = evidence.data.get('unparsed', [])
    own_library_file_count: int = evidence.data.get('own_library_file_count', 0)

    if mutations:
        return ItemAssessment(
            checklist_id=checklist_id,
            verdict=Verdict.FAIL,
            rationale=(
                f"{len(mutations)} location(s) in the charm's own published library set "
                f'unit or application status directly, rather than returning a value or '
                f'raising an exception for the charm to handle.'
            ),
            evidence=evidence.lines,
        )

    if unparsed:
        # A file that could not be read might contain the very mutation this
        # item is looking for - "the library does not mutate status" is not a
        # claim this check can make while some of it went unread.
        return ItemAssessment(
            checklist_id=checklist_id,
            verdict=Verdict.NEEDS_HUMAN,
            rationale=(
                f"No status mutation was found in the charm's own published library, but "
                f'{len(unparsed)} file(s) could not be parsed.'
            ),
            evidence=[f'{path}: could not be parsed' for path in unparsed],
        )

    if not own_library_file_count:
        return ItemAssessment(
            checklist_id=checklist_id,
            verdict=Verdict.NOT_APPLICABLE,
            rationale='The charm does not publish its own library under lib/charms/.',
        )

    return ItemAssessment(
        checklist_id=checklist_id,
        verdict=Verdict.PASS,
        rationale=(
            f"None of the charm's {own_library_file_count} own library file(s) set unit or "
            f'application status directly.'
        ),
    )


library_status_mutation = ItemCheck(
    checklist_id='best-practice-libraries-no-status-mutation',
    gather=gather_library_status_mutation,
    decide=decide_library_status_mutation,
)


# --- charmcraft-yaml-key-documentation --------------------------------------

# This item's evidence, like item 23's and item 25's, is not one of
# canonical/operator's best-practice admonitions with a `:name:` anchor - it
# is charmcraft's own `links` file-reference page. The checklist_id below is
# this module's own placeholder, reusing the referenced sphinx target name
# verbatim (`{external+charmcraft:ref}` links <charmcraft-yaml-key-documentation>``
# in the live best-practices.txt), the same precedent item 23's
# `charmcraft-yaml-key-resources` set.
#
# See PLAN.md's design note for why "documentation and website resolve to
# the same domain" is the one confident violation shape this check can name
# without positively confirming a documentation link's content: it rules out
# "documentation is just pointing back at the vendor's own site", it does
# not rule in "this is definitely charm-specific content".


def _link_value(links: dict[str, Any], key: str) -> str:
    """Read a `links.<key>` value, which may be a bare string or a list."""
    value = links.get(key)
    if isinstance(value, list):
        return str(value[0]) if value else ''
    return str(value or '')


def _domain(url: str) -> str:
    """The registrable-ish domain of a URL, for a cheap same-site comparison."""
    host = urlparse(url).netloc.lower()
    return host.removeprefix('www.')


def gather_documentation_link(source: CharmSource) -> Evidence:
    """Collect the `documentation` and `website` links, nothing else."""
    charmcraft = source.charmcraft_yaml()
    links = charmcraft.get('links')
    links = links if isinstance(links, dict) else {}
    documentation = _link_value(links, 'documentation')
    website = _link_value(links, 'website')

    lines = []
    if documentation:
        lines.append(f'documentation: {documentation}')
    if website:
        lines.append(f'website: {website}')
    return Evidence(lines=lines, data={'documentation': documentation, 'website': website})


def decide_documentation_link(evidence: Evidence) -> ItemAssessment:
    """Rule on whether `documentation` looks like it is really `website`.

    One confident branch: `documentation` and `website` resolve to the same
    domain, which means `documentation` is not pointing anywhere different
    from the charmed application's own site. Everything else - no
    `documentation` link at all, or one that is on a different domain from
    `website` (or there is no `website` to compare against) - defers, per
    the module's standing rule that a check should not guess a PASS it
    cannot back up.
    """
    checklist_id = 'charmcraft-yaml-key-documentation'
    documentation: str = evidence.data.get('documentation', '')
    website: str = evidence.data.get('website', '')

    if not documentation:
        return ItemAssessment(
            checklist_id=checklist_id,
            verdict=Verdict.NOT_APPLICABLE,
            rationale='The charm declares no `documentation` link.',
        )

    documentation_domain = _domain(documentation)
    if website and documentation_domain and documentation_domain == _domain(website):
        return ItemAssessment(
            checklist_id=checklist_id,
            verdict=Verdict.FAIL,
            rationale=(
                f'`documentation` ({documentation}) and `website` ({website}) resolve to the '
                f'same domain, so `documentation` does not look like charm-specific content.'
            ),
            evidence=evidence.lines,
        )

    if website:
        rationale = (
            f'`documentation` ({documentation}) is on a different domain than `website` '
            f'({website}); whether its content is actually about the charm still needs a '
            f'human to check.'
        )
    else:
        rationale = (
            f'`documentation` ({documentation}) is set, but there is no `website` link to '
            f'compare it against; whether its content is about the charm needs a human '
            f'to check.'
        )
    return ItemAssessment(
        checklist_id=checklist_id,
        verdict=Verdict.NEEDS_HUMAN,
        rationale=rationale,
        evidence=evidence.lines,
    )


documentation_link = ItemCheck(
    checklist_id='charmcraft-yaml-key-documentation',
    gather=gather_documentation_link,
    decide=decide_documentation_link,
)


ITEM_CHECKS: dict[str, ItemCheck] = {
    check.checklist_id: check
    for check in (
        safe_subprocess,
        automated_releasing,
        integration_tests,
        no_duplicate_model_config,
        dependency_update_tooling,
        avoid_charm_plugin,
        pin_workload_versions,
        summary_description,
        logging_not_print,
        sensitive_logging,
        resource_architectures,
        library_status_mutation,
        documentation_link,
    )
}
