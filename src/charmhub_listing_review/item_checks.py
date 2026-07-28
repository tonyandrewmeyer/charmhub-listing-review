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

"""Per-item checks for checklist entries that ``evaluate.py`` leaves blank.

``evaluate.py`` holds the checks that decide an item from metadata: does a
file exist, does a URL resolve, does a YAML key have the right shape. The
items that remain unticked are the ones that need the charm's *source* read,
and this module is where those live.

Each item is an :class:`ItemCheck` with two halves:

* ``gather`` collects the evidence the item names, and nothing else. It is
  deliberately separate so that the same evidence feeds both halves below,
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
class Evidence:
    """What ``gather`` found, in a form both a human and a model can read."""

    lines: list[str] = dataclasses.field(default_factory=list)
    """Human-readable evidence, one line per finding or call site."""

    data: dict[str, Any] = dataclasses.field(default_factory=dict)
    """Structured evidence for ``decide`` to rule on."""


@dataclasses.dataclass
class ItemCheck:
    """A checklist item that is decided by reading the charm's source."""

    checklist_id: str
    """The ``<!-- id: ... -->`` slug this check answers."""

    gather: Callable[[pathlib.Path, str], Evidence]
    """Collect the evidence this item names. Takes (charm_path, charm_name)."""

    decide: Callable[[Evidence], ItemAssessment]
    """Rule on gathered evidence, deferring what it cannot settle."""

    def assess(self, charm_path: pathlib.Path, charm_name: str = '') -> ItemAssessment:
        """Gather evidence for this item and rule on it."""
        return self.decide(self.gather(charm_path, charm_name))


# --- best-practice-safe-subprocess -------------------------------------------

# Callables that start an external process. ``subprocess`` covers machine
# charms; ``Container.exec`` is how a Kubernetes charm runs anything at all,
# and a charm can easily contain no ``subprocess`` call while running commands
# throughout via Pebble.
_SUBPROCESS_FUNCTIONS = frozenset({'run', 'call', 'check_call', 'check_output', 'Popen'})

# Programs that take a command *string* and hand it to a shell, so that passing
# a list to exec() still ends up shell-interpreted.
_SHELL_WRAPPERS = frozenset({'sh', 'bash', 'dash', 'zsh', 'su', 'sudo', 'env'})


@dataclasses.dataclass
class _ExecSite:
    """One place the charm starts an external process."""

    location: str
    """``path:lineno``, relative to the charm directory."""

    kind: str
    """``'subprocess'``, ``'os.system'`` or ``'container.exec'``."""

    problems: list[str] = dataclasses.field(default_factory=list)
    """Best-practice violations found at this site."""

    undecidable: str = ''
    """Why this site could not be ruled on, if it could not."""


class _ExecVisitor(ast.NodeVisitor):
    """Collect external-process call sites and what is wrong with them."""

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
        func = node.func
        if isinstance(func, ast.Attribute):
            if _is_name(func.value, 'subprocess') and func.attr in _SUBPROCESS_FUNCTIONS:
                self._add_subprocess(node, func.attr)
            elif _is_name(func.value, 'os') and func.attr == 'system':
                self.sites.append(
                    _ExecSite(
                        location=self._location(node),
                        kind='os.system',
                        problems=['runs a command through the shell (os.system)'],
                    )
                )
            elif func.attr == 'exec':
                self._add_container_exec(node)
        self.generic_visit(node)

    def _location(self, node: ast.AST) -> str:
        return f'{self._path}:{getattr(node, "lineno", 0)}'

    def _add_subprocess(self, node: ast.Call, attr: str) -> None:
        site = _ExecSite(location=self._location(node), kind='subprocess')
        keywords = {kw.arg: kw.value for kw in node.keywords if kw.arg}

        shell = keywords.get('shell')
        if isinstance(shell, ast.Constant) and shell.value is True:
            site.problems.append('passes shell=True')

        argv = self._resolve(node.args[0]) if node.args else None
        _check_argv(site, argv)

        # check= and capture_output= only apply to the high-level helpers;
        # check_call/check_output/Popen have their own semantics.
        if attr == 'run':
            check = keywords.get('check')
            if not (isinstance(check, ast.Constant) and check.value is True):
                site.problems.append('does not pass check=True, so failures pass silently')
            if not ({'capture_output', 'stdout', 'stderr'} & set(keywords)):
                site.problems.append('does not capture output, so it leaks into the Juju log')
        self.sites.append(site)

    def _add_container_exec(self, node: ast.Call) -> None:
        """Handle ``<container>.exec([...])``.

        Any ``.exec()`` attribute call is treated as an ops ``Container.exec``.
        The name of the receiver varies too much across charms
        (``self.container``, ``self._container``, a local, a parameter) to
        match on, and no other common API spells a method ``exec``.
        """
        site = _ExecSite(location=self._location(node), kind='container.exec')
        _check_argv(site, self._resolve(node.args[0]) if node.args else None)
        if id(node) in self._discarded:
            site.problems.append(
                'discards the ExecProcess without waiting, so a failure is never noticed'
            )
        self.sites.append(site)


def _is_name(node: ast.AST, name: str) -> bool:
    return isinstance(node, ast.Name) and node.id == name


def _check_argv(site: _ExecSite, argv: ast.AST | None) -> None:
    """Check the command argument for shell strings and relative paths."""
    if argv is None:
        site.undecidable = 'no positional command argument'
        return

    # A bare string command is shell-interpreted (subprocess with shell=True)
    # or simply wrong (Container.exec wants a list).
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


def gather_exec_sites(charm_path: pathlib.Path, charm_name: str = '') -> Evidence:
    """Collect every place the charm starts an external process."""
    sites: list[_ExecSite] = []
    unparsed: list[str] = []
    for path in first_party_python_files(charm_path, charm_name):
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
    return Evidence(lines=lines, data={'sites': sites, 'unparsed': unparsed})


def decide_safe_subprocess(evidence: Evidence) -> ItemAssessment:
    """Rule on the gathered call sites."""
    sites: list[_ExecSite] = evidence.data.get('sites', [])
    unparsed: list[str] = evidence.data.get('unparsed', [])
    checklist_id = 'best-practice-safe-subprocess'

    if not sites:
        if unparsed:
            return ItemAssessment(
                checklist_id=checklist_id,
                verdict=Verdict.NEEDS_HUMAN,
                rationale=(
                    f'Found no external-process calls, but {len(unparsed)} file(s) '
                    f'could not be parsed.'
                ),
                evidence=[f'{path}: could not be parsed' for path in unparsed],
            )
        return ItemAssessment(
            checklist_id=checklist_id,
            verdict=Verdict.NOT_APPLICABLE,
            rationale='The charm does not run any external commands.',
        )

    failing = [site for site in sites if site.problems]
    if failing:
        return ItemAssessment(
            checklist_id=checklist_id,
            verdict=Verdict.FAIL,
            rationale=(
                f'{len(failing)} of {len(sites)} external-process call(s) do not follow '
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
                f'{len(undecidable)} of {len(sites)} external-process call(s) build their '
                f'command somewhere the check cannot see.'
            ),
            evidence=evidence.lines,
        )

    return ItemAssessment(
        checklist_id=checklist_id,
        verdict=Verdict.PASS,
        rationale=(
            f'All {len(sites)} external-process call(s) use absolute paths, pass arguments '
            f'as a list, and handle failure.'
        ),
    )


safe_subprocess = ItemCheck(
    checklist_id='best-practice-safe-subprocess',
    gather=gather_exec_sites,
    decide=decide_safe_subprocess,
)

ITEM_CHECKS: dict[str, ItemCheck] = {check.checklist_id: check for check in (safe_subprocess,)}
