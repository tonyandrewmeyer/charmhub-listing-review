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

"""Tests for the per-item source-reading checks."""

import pathlib

import pytest

from charmhub_listing_review._models import ItemAssessment, Verdict
from charmhub_listing_review.item_checks import (
    ITEM_CHECKS,
    CharmSource,
    Evidence,
    ItemCheck,
    first_party_python_files,
    safe_subprocess,
)


def _charm(tmp_path: pathlib.Path, **files: str) -> pathlib.Path:
    """Write ``files`` (relative path -> source) into a charm directory."""
    for name, source in files.items():
        path = tmp_path / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(source)
    return tmp_path


def _assess(tmp_path: pathlib.Path, charm_name: str = 'my-charm', **files: str):
    return safe_subprocess.assess(
        CharmSource(charm_path=_charm(tmp_path, **files), charm_name=charm_name)
    )


class TestFirstPartyPythonFiles:
    def test_excludes_vendored_charm_libs(self, tmp_path: pathlib.Path):
        charm = _charm(
            tmp_path,
            **{
                'src/charm.py': '',
                'lib/charms/data_platform_libs/v0/data_interfaces.py': '',
                'lib/charms/loki_k8s/v1/loki_push_api.py': '',
            },
        )
        found = [p.relative_to(charm).as_posix() for p in first_party_python_files(charm)]
        assert found == ['src/charm.py']

    def test_includes_the_charms_own_published_library(self, tmp_path: pathlib.Path):
        charm = _charm(
            tmp_path,
            **{
                'src/charm.py': '',
                'lib/charms/my_charm/v0/my_lib.py': '',
                'lib/charms/someone_else/v0/their_lib.py': '',
            },
        )
        found = [
            p.relative_to(charm).as_posix() for p in first_party_python_files(charm, 'my-charm')
        ]
        assert found == ['lib/charms/my_charm/v0/my_lib.py', 'src/charm.py']

    def test_excludes_tests(self, tmp_path: pathlib.Path):
        charm = _charm(tmp_path, **{'src/charm.py': '', 'tests/unit/test_charm.py': ''})
        found = [p.relative_to(charm).as_posix() for p in first_party_python_files(charm)]
        assert found == ['src/charm.py']


class TestSafeSubprocess:
    """Item 17, the half ruff cannot see: ``ops.Container.exec``.

    The ``subprocess`` half is ruff's (S602/S604/S605/S607, PLW1510), so
    there are no tests for it here.
    """

    def test_no_commands_is_not_applicable(self, tmp_path: pathlib.Path):
        """A charm that runs nothing has nothing to get wrong."""
        assessment = _assess(tmp_path, **{'src/charm.py': 'import ops\n'})
        assert assessment.verdict is Verdict.NOT_APPLICABLE
        assert not assessment.evidence

    def test_subprocess_is_left_to_ruff(self, tmp_path: pathlib.Path):
        """A `subprocess` call is not this check's business, however bad it is."""
        assessment = _assess(
            tmp_path,
            **{'src/charm.py': "import subprocess\nsubprocess.run('apt update', shell=True)\n"},
        )
        assert assessment.verdict is Verdict.NOT_APPLICABLE

    def test_container_exec_is_checked(self, tmp_path: pathlib.Path):
        """A Kubernetes charm runs commands without importing subprocess."""
        assessment = _assess(
            tmp_path,
            **{
                'src/charm.py': (
                    "import ops\nself.container.exec(['chown', 'git:git', '/data'])\n"
                )
            },
        )
        assert assessment.verdict is Verdict.FAIL
        assert any("runs 'chown' by a relative name" in line for line in assessment.evidence)
        assert any('discards the ExecProcess' in line for line in assessment.evidence)

    def test_container_exec_awaited_is_not_flagged_for_discarding(self, tmp_path: pathlib.Path):
        assessment = _assess(
            tmp_path,
            **{
                'src/charm.py': (
                    "import ops\noutput, _ = container.exec(['/usr/bin/id']).wait_output()\n"
                )
            },
        )
        assert assessment.verdict is Verdict.PASS

    @pytest.mark.parametrize(
        'command,expected',
        [
            ("'chown git /data'", 'as a string rather than a list'),
            ("f'chown {user} /data'", 'as an f-string rather than a list'),
            ("['chown', '/data']", 'by a relative name'),
        ],
    )
    def test_container_exec_violations(self, tmp_path: pathlib.Path, command: str, expected: str):
        assessment = _assess(
            tmp_path,
            **{'src/charm.py': f'import ops\noutput, _ = c.exec({command}).wait_output()\n'},
        )
        assert assessment.verdict is Verdict.FAIL
        assert any(expected in line for line in assessment.evidence)

    def test_shell_wrapper_in_a_list_is_still_a_shell(self, tmp_path: pathlib.Path):
        """``['su', 'git', '-c', cmd]`` is a list, but a shell still parses cmd."""
        assessment = _assess(
            tmp_path,
            **{
                'src/actions.py': (
                    'import ops\n'
                    "output, _ = container.exec(['su', 'git', '-c', cmd]).wait_output()\n"
                )
            },
        )
        assert assessment.verdict is Verdict.FAIL
        assert any('the shell interprets it' in line for line in assessment.evidence)

    def test_follows_a_command_assigned_to_a_local(self, tmp_path: pathlib.Path):
        """Charms build the command a line before running it; look through that."""
        assessment = _assess(
            tmp_path,
            **{
                'src/actions.py': (
                    'def exec_as_git(container, cmd):\n'
                    "    argv = ['su', 'git', '-c', cmd]\n"
                    '    return container.exec(argv).wait_output()\n'
                )
            },
        )
        assert assessment.verdict is Verdict.FAIL
        assert any('the shell interprets it' in line for line in assessment.evidence)

    def test_does_not_follow_a_name_assigned_more_than_once(self, tmp_path: pathlib.Path):
        """Resolving a reassigned name would be guessing which value applies."""
        assessment = _assess(
            tmp_path,
            **{
                'src/charm.py': (
                    'def run(container, safe):\n'
                    "    argv = ['/usr/bin/id']\n"
                    "    argv = ['/usr/bin/whoami']\n"
                    '    return container.exec(argv).wait_output()\n'
                )
            },
        )
        assert assessment.verdict is Verdict.NEEDS_HUMAN

    def test_command_built_elsewhere_needs_a_human(self, tmp_path: pathlib.Path):
        assessment = _assess(
            tmp_path,
            **{'src/charm.py': 'import ops\ncontainer.exec(argv).wait_output()\n'},
        )
        assert assessment.verdict is Verdict.NEEDS_HUMAN
        assert any('built elsewhere' in line for line in assessment.evidence)

    def test_violations_outrank_undecidable_calls(self, tmp_path: pathlib.Path):
        """A definite failure is reported even when another call is unclear."""
        assessment = _assess(
            tmp_path,
            **{
                'src/charm.py': (
                    'import ops\n'
                    'container.exec(argv).wait_output()\n'
                    "container.exec('rm -rf /tmp/x').wait_output()\n"
                )
            },
        )
        assert assessment.verdict is Verdict.FAIL

    def test_findings_in_vendored_libs_are_not_the_charms_problem(self, tmp_path: pathlib.Path):
        assessment = _assess(
            tmp_path,
            **{
                'src/charm.py': 'import ops\n',
                'lib/charms/someone_else/v0/their_lib.py': (
                    "import ops\ncontainer.exec(['curl', 'example.com'])\n"
                ),
            },
        )
        assert assessment.verdict is Verdict.NOT_APPLICABLE

    def test_unparseable_source_is_not_silently_a_pass(self, tmp_path: pathlib.Path):
        assessment = _assess(tmp_path, **{'src/charm.py': 'def broken(:\n'})
        assert assessment.verdict is Verdict.NEEDS_HUMAN

    def test_unparseable_source_caps_an_otherwise_clean_charm(self, tmp_path: pathlib.Path):
        """The file that did not parse is where an unexamined call would be."""
        assessment = _assess(
            tmp_path,
            **{
                'src/charm.py': (
                    "import ops\noutput, _ = container.exec(['/usr/bin/id']).wait_output()\n"
                ),
                'src/other.py': 'def broken(:\n',
            },
        )
        assert assessment.verdict is Verdict.NEEDS_HUMAN
        assert any('other.py: could not be parsed' in line for line in assessment.evidence)

    def test_registered_under_its_checklist_id(self):
        assert ITEM_CHECKS['best-practice-safe-subprocess'] is safe_subprocess


class TestUnreadInputsCapTheVerdict:
    """An item cannot be settled by evidence it could not read."""

    def _check(self, verdict: Verdict, unread: list[str]) -> ItemCheck:
        return ItemCheck(
            checklist_id='example',
            gather=lambda source: Evidence(lines=['looked'], unread=unread),
            decide=lambda evidence: ItemAssessment(
                checklist_id='example',
                verdict=verdict,
                rationale='The charm is fine.',
                evidence=evidence.lines,
            ),
        )

    @pytest.mark.parametrize('verdict', [Verdict.PASS, Verdict.FAIL, Verdict.NOT_APPLICABLE])
    def test_a_settled_verdict_is_capped(self, tmp_path: pathlib.Path, verdict: Verdict):
        check = self._check(verdict, ['src/other.py: could not be parsed'])
        assessment = check.assess(CharmSource(charm_path=tmp_path))
        assert assessment.verdict is Verdict.NEEDS_HUMAN
        assert 'could not be read' in assessment.rationale
        assert 'src/other.py: could not be parsed' in assessment.evidence

    def test_nothing_unread_is_left_alone(self, tmp_path: pathlib.Path):
        check = self._check(Verdict.PASS, [])
        assessment = check.assess(CharmSource(charm_path=tmp_path))
        assert assessment.verdict is Verdict.PASS
        assert assessment.rationale == 'The charm is fine.'
