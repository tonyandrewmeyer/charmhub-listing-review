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

"""Tests for reading GitHub Actions workflows.

The consuming checklist items are tested through `item_checks`, but the two
things here that would fail quietly belong to this module: the `on:` key
arriving as the boolean `True` from PyYAML, and the fixed-point resolution of
`workflow_call` chains. A failure in either reads as a workflow with no
triggers at all, which looks like a charm that does not run CI rather than
like a parser bug.
"""

import pathlib

import pytest

from charmhub_listing_review._workflows import (
    load_workflows,
    resolve_triggers,
    step_commands,
)


def _repo(tmp_path: pathlib.Path, **files: str) -> pathlib.Path:
    workflow_dir = tmp_path / '.github' / 'workflows'
    workflow_dir.mkdir(parents=True)
    for name, content in files.items():
        (workflow_dir / name).write_text(content, encoding='utf-8')
    return tmp_path


def _summary(tmp_path: pathlib.Path, content: str, default_branch: str = 'main'):
    workflows = load_workflows(_repo(tmp_path, **{'ci.yaml': content}))
    return resolve_triggers(workflows, default_branch)['.github/workflows/ci.yaml']


class TestTriggerNormalisation:
    @pytest.mark.parametrize(
        'on_block',
        [
            'on: push',
            'on: [push]',
            'on:\n  push:',
            '"on":\n  push:',
        ],
    )
    def test_every_spelling_of_on_is_read(self, tmp_path: pathlib.Path, on_block: str):
        """PyYAML resolves an unquoted `on` to True, so both keys must work."""
        summary = _summary(tmp_path, f'name: CI\n{on_block}\njobs: {{}}\n')
        assert 'push' in summary.events

    def test_a_workflow_with_no_triggers_has_none(self, tmp_path: pathlib.Path):
        summary = _summary(tmp_path, 'name: CI\njobs: {}\n')
        assert summary.events == set()
        assert not summary.default_branch_push


class TestDefaultBranchPush:
    @pytest.mark.parametrize(
        'push_block,expected',
        [
            ('push:', True),
            ("push:\n    branches: ['main']", True),
            ("push:\n    branches: ['**']", True),
            ("push:\n    branches: ['feature/*']", False),
            ("push:\n    branches-ignore: ['main']", False),
            ("push:\n    branches-ignore: ['feature/*']", True),
            # `['**', '!main']` is how "every branch except the default one"
            # is written, and GitHub does not run it on main.
            ("push:\n    branches: ['**', '!main']", False),
            ("push:\n    branches: ['**', '!feature/*']", True),
            ("push:\n    branches: ['!main']", False),
        ],
    )
    def test_branch_filters(self, tmp_path: pathlib.Path, push_block: str, expected: bool):
        summary = _summary(tmp_path, f'name: CI\non:\n  {push_block}\njobs: {{}}\n')
        assert summary.default_branch_push is expected

    def test_a_path_filter_is_a_caveat_not_a_no(self, tmp_path: pathlib.Path):
        """It still runs on the default branch, just not for every change."""
        summary = _summary(
            tmp_path,
            "name: CI\non:\n  push:\n    branches: ['main']\n    paths: ['src/**']\njobs: {}\n",
        )
        assert summary.default_branch_push
        assert any('paths' in caveat for caveat in summary.caveats)


class TestWorkflowCall:
    def test_triggers_are_inherited_through_a_chain(self, tmp_path: pathlib.Path):
        """A called workflow runs exactly when its callers run."""
        repo = _repo(
            tmp_path,
            **{
                'top.yaml': (
                    "name: Top\non:\n  push:\n    branches: ['main']\n"
                    'jobs:\n  call:\n    uses: ./.github/workflows/middle.yaml\n'
                ),
                'middle.yaml': (
                    'name: Middle\non:\n  workflow_call:\n'
                    'jobs:\n  call:\n    uses: ./.github/workflows/leaf.yaml\n'
                ),
                'leaf.yaml': 'name: Leaf\non:\n  workflow_call:\njobs: {}\n',
            },
        )
        summaries = resolve_triggers(load_workflows(repo), 'main')
        leaf = summaries['.github/workflows/leaf.yaml']
        assert 'push' in leaf.events
        assert leaf.default_branch_push
        assert '.github/workflows/middle.yaml' in leaf.called_by

    def test_a_caller_that_does_not_run_on_the_default_branch_does_not_confer_it(
        self, tmp_path: pathlib.Path
    ):
        repo = _repo(
            tmp_path,
            **{
                'top.yaml': (
                    "name: Top\non:\n  push:\n    branches: ['release/*']\n"
                    'jobs:\n  call:\n    uses: ./.github/workflows/leaf.yaml\n'
                ),
                'leaf.yaml': 'name: Leaf\non:\n  workflow_call:\njobs: {}\n',
            },
        )
        summaries = resolve_triggers(load_workflows(repo), 'main')
        assert not summaries['.github/workflows/leaf.yaml'].default_branch_push

    def test_a_cycle_terminates(self, tmp_path: pathlib.Path):
        """GitHub rejects these, but the fixed point must not spin on one."""
        repo = _repo(
            tmp_path,
            **{
                'a.yaml': (
                    'name: A\non:\n  workflow_call:\n'
                    'jobs:\n  call:\n    uses: ./.github/workflows/b.yaml\n'
                ),
                'b.yaml': (
                    'name: B\non:\n  workflow_call:\n'
                    'jobs:\n  call:\n    uses: ./.github/workflows/a.yaml\n'
                ),
            },
        )
        summaries = resolve_triggers(load_workflows(repo), 'main')
        assert not summaries['.github/workflows/a.yaml'].default_branch_push


class TestLoading:
    def test_unparseable_yaml_is_recorded_not_raised(self, tmp_path: pathlib.Path):
        workflows = load_workflows(_repo(tmp_path, **{'ci.yaml': 'name: [\n'}))
        assert workflows[0].unreadable

    def test_a_repository_with_no_workflows(self, tmp_path: pathlib.Path):
        assert load_workflows(tmp_path) == []


class TestStepCommands:
    def test_a_continued_command_arrives_whole(self, tmp_path: pathlib.Path):
        """Release invocations carry enough flags to be split over lines."""
        workflows = load_workflows(
            _repo(
                tmp_path,
                **{
                    'release.yaml': (
                        'name: Release\non:\n  push:\njobs:\n  release:\n    steps:\n'
                        '      - run: |\n'
                        '          charmcraft upload \\\n'
                        '            --release edge foo.charm\n'
                    )
                },
            )
        )
        commands = [command for _, command in step_commands(workflows[0])]
        assert 'charmcraft upload --release edge foo.charm' in commands

    def test_comments_and_blank_lines_are_dropped(self, tmp_path: pathlib.Path):
        workflows = load_workflows(
            _repo(
                tmp_path,
                **{
                    'ci.yaml': (
                        'name: CI\non:\n  push:\njobs:\n  test:\n    steps:\n'
                        '      - run: |\n'
                        '          # build it\n'
                        '\n'
                        '          make test\n'
                    )
                },
            )
        )
        commands = [command for _, command in step_commands(workflows[0])]
        assert commands == ['make test']
