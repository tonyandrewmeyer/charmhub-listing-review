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
    CharmSource,
    Evidence,
    ItemCheck,
    first_party_python_files,
)


def _charm(tmp_path: pathlib.Path, **files: str) -> pathlib.Path:
    """Write ``files`` (relative path -> source) into a charm directory."""
    for name, source in files.items():
        path = tmp_path / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(source)
    return tmp_path


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
