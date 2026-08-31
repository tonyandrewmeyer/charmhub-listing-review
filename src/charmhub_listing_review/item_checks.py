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
import pathlib
from collections.abc import Callable
from typing import Any

import yaml

from ._models import ItemAssessment, Verdict

# Vendored charm libraries live here. They are third-party code that the charm
# author did not write and cannot change, so findings in them are not the
# charm's to answer for - see ``first_party_python_files``.
_LIB_CHARMS = ('lib', 'charms')

# Directories whose contents are not the charm's runtime behaviour.
_EXCLUDED_DIRS = frozenset({'tests', 'test', '.git', '.tox', 'venv', '.venv', 'build'})


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


ITEM_CHECKS: dict[str, ItemCheck] = {check.checklist_id: check for check in (safe_subprocess,)}
