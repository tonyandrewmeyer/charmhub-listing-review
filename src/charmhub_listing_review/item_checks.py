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
