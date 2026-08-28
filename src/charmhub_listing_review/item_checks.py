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

import dataclasses
import pathlib
from collections.abc import Callable
from typing import Any

import yaml

from ._models import ItemAssessment

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
    """The directory holding ``charmcraft.yaml``."""

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


@dataclasses.dataclass
class ItemCheck:
    """A checklist item that is decided by reading the charm's source."""

    checklist_id: str
    """The ``<!-- id: ... -->`` slug this check answers."""

    gather: Callable[[CharmSource], Evidence]
    """Collect the evidence this item names, and nothing else."""

    decide: Callable[[Evidence], ItemAssessment]
    """Rule on gathered evidence, deferring what it cannot settle."""

    def assess(self, source: CharmSource) -> ItemAssessment:
        """Gather evidence for this item and rule on it."""
        return self.decide(self.gather(source))
