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
    action_additional_properties,
    automated_releasing,
    avoid_charm_plugin,
    dependency_update_tooling,
    first_party_python_files,
    integration_tests,
    no_duplicate_model_config,
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


def _source(tmp_path: pathlib.Path, charm_name: str = 'my-charm', **files: str) -> CharmSource:
    return CharmSource(charm_path=_charm(tmp_path, **files), charm_name=charm_name)


_RELEASE_ON_PUSH = """
name: Release
on:
  push:
    branches: [main]
jobs:
  release:
    runs-on: ubuntu-latest
    steps:
      - run: charmcraft upload --release edge my.charm
"""

_INTEGRATION_ON_PUSH = """
name: Integration
on:
  push:
    branches: [main]
jobs:
  integration:
    runs-on: ubuntu-latest
    steps:
      - run: tox -e integration
"""


class TestAutomatedReleasing:
    def test_no_workflows_at_all_fails(self, tmp_path: pathlib.Path):
        assessment = automated_releasing.assess(_source(tmp_path))
        assert assessment.verdict is Verdict.FAIL
        assert 'no GitHub Actions workflows' in assessment.rationale

    def test_publishing_on_default_branch_push_passes(self, tmp_path: pathlib.Path):
        assessment = automated_releasing.assess(
            _source(tmp_path, **{'.github/workflows/release.yaml': _RELEASE_ON_PUSH})
        )
        assert assessment.verdict is Verdict.PASS

    def test_workflows_that_never_publish_fail(self, tmp_path: pathlib.Path):
        workflow = _RELEASE_ON_PUSH.replace('charmcraft upload --release edge my.charm', 'tox')
        assessment = automated_releasing.assess(
            _source(tmp_path, **{'.github/workflows/ci.yaml': workflow})
        )
        assert assessment.verdict is Verdict.FAIL
        assert 'No workflow publishes' in assessment.rationale

    def test_manual_dispatch_only_is_a_judgement_call(self, tmp_path: pathlib.Path):
        """Releasing may be driven from outside GitHub, as a Launchpad mirror is."""
        workflow = _RELEASE_ON_PUSH.replace('push:\n    branches: [main]', 'workflow_dispatch:')
        assessment = automated_releasing.assess(
            _source(tmp_path, **{'.github/workflows/release.yaml': workflow})
        )
        assert assessment.verdict is Verdict.NEEDS_HUMAN
        assert 'none runs on a push to the default branch' in assessment.rationale

    def test_a_reusable_workflow_counts_as_publishing(self, tmp_path: pathlib.Path):
        workflow = """
on:
  push:
    branches: [main]
jobs:
  release:
    uses: canonical/charming-actions/.github/workflows/release-charm.yaml@2.7.0
"""
        assessment = automated_releasing.assess(
            _source(tmp_path, **{'.github/workflows/release.yaml': workflow})
        )
        assert assessment.verdict is Verdict.PASS

    def test_publishing_only_to_stable_is_not_an_unstable_release(self, tmp_path: pathlib.Path):
        workflow = _RELEASE_ON_PUSH.replace('--release edge', '--release stable')
        assessment = automated_releasing.assess(
            _source(tmp_path, **{'.github/workflows/release.yaml': workflow})
        )
        assert assessment.verdict is Verdict.NEEDS_HUMAN
        assert 'not unstable channels' in assessment.rationale

    def test_a_path_filter_means_not_every_change(self, tmp_path: pathlib.Path):
        workflow = _RELEASE_ON_PUSH.replace(
            'branches: [main]', "branches: [main]\n    paths: ['src/**']"
        )
        assessment = automated_releasing.assess(
            _source(tmp_path, **{'.github/workflows/release.yaml': workflow})
        )
        assert assessment.verdict is Verdict.NEEDS_HUMAN
        assert 'paths filter' in assessment.rationale

    def test_a_reusable_workflow_inherits_its_callers_triggers(self, tmp_path: pathlib.Path):
        """`workflow_call` has no triggers of its own; the caller's decide it."""
        called = """
on:
  workflow_call:
jobs:
  release:
    runs-on: ubuntu-latest
    steps:
      - run: charmcraft upload --release edge my.charm
"""
        caller = """
on:
  push:
    branches: [main]
jobs:
  call:
    uses: ./.github/workflows/release.yaml
"""
        assessment = automated_releasing.assess(
            _source(
                tmp_path,
                **{
                    '.github/workflows/release.yaml': called,
                    '.github/workflows/ci.yaml': caller,
                },
            )
        )
        assert assessment.verdict is Verdict.PASS

    def test_an_uncalled_reusable_workflow_never_runs(self, tmp_path: pathlib.Path):
        called = """
on:
  workflow_call:
jobs:
  release:
    runs-on: ubuntu-latest
    steps:
      - run: charmcraft upload --release edge my.charm
"""
        assessment = automated_releasing.assess(
            _source(tmp_path, **{'.github/workflows/release.yaml': called})
        )
        assert assessment.verdict is Verdict.NEEDS_HUMAN

    def test_quoted_and_shorthand_on_keys_are_both_read(self, tmp_path: pathlib.Path):
        """YAML 1.1 turns an unquoted `on` into the boolean True."""
        quoted = _RELEASE_ON_PUSH.replace('on:\n  push:\n    branches: [main]', "'on':\n  push:")
        assessment = automated_releasing.assess(
            _source(tmp_path, **{'.github/workflows/release.yaml': quoted})
        )
        assert assessment.verdict is Verdict.PASS

    def test_an_unrecognised_reusable_workflow_is_not_evidence_of_absence(
        self, tmp_path: pathlib.Path
    ):
        """A workflow in another repository may publish without saying so."""
        workflow = """
on:
  push:
    branches: [main]
jobs:
  ci:
    uses: canonical/charm-ci/.github/workflows/do-everything.yml@v1
"""
        assessment = automated_releasing.assess(
            _source(tmp_path, **{'.github/workflows/ci.yaml': workflow})
        )
        assert assessment.verdict is Verdict.NEEDS_HUMAN
        assert 'reusable workflows in other repositories' in assessment.rationale

    def test_publishing_reusable_workflows_are_named_both_ways_round(self, tmp_path: pathlib.Path):
        """`charm-release.yaml` and `publish-artifacts.yml` both publish."""
        for name in (
            'canonical/observability/.github/workflows/charm-release.yaml@v2',
            'canonical/charm-ci/.github/workflows/publish-artifacts.yml@v1',
        ):
            workflow = f'on:\n  push:\n    branches: [main]\njobs:\n  r:\n    uses: {name}\n'
            assessment = automated_releasing.assess(
                _source(tmp_path, **{'.github/workflows/release.yaml': workflow})
            )
            assert assessment.verdict is Verdict.PASS, name

    def test_a_tool_install_channel_is_not_the_release_channel(self, tmp_path: pathlib.Path):
        """`snap install charmcraft --channel latest/stable` is not a release."""
        workflow = (
            'name: Release\n'
            'on:\n'
            '  push:\n'
            '    branches: [main]\n'
            'jobs:\n'
            '  release:\n'
            '    steps:\n'
            '      - run: sudo snap install charmcraft --classic --channel latest/stable\n'
            '      - run: charmcraft upload --release ${{ inputs.channel }} my.charm\n'
        )
        assessment = automated_releasing.assess(
            _source(tmp_path, **{'.github/workflows/release.yaml': workflow})
        )
        assert assessment.verdict is Verdict.PASS

    def test_an_unreadable_workflow_is_not_an_absent_one(self, tmp_path: pathlib.Path):
        assessment = automated_releasing.assess(
            _source(tmp_path, **{'.github/workflows/release.yaml': 'name: [\n'})
        )
        assert assessment.verdict is Verdict.NEEDS_HUMAN
        assert 'could not be read' in assessment.rationale

    def test_registered_under_its_checklist_id(self):
        assert ITEM_CHECKS['ci-automated-releasing'] is automated_releasing


_CHARMCRAFT_WITH_ENDPOINTS = """
name: my-charm
requires:
  database:
    interface: postgresql_client
  logging:
    interface: loki_push_api
provides:
  metrics-endpoint:
    interface: prometheus_scrape
"""


class TestIntegrationTests:
    def test_no_integration_tests_fails(self, tmp_path: pathlib.Path):
        assessment = integration_tests.assess(
            _source(tmp_path, **{'charmcraft.yaml': _CHARMCRAFT_WITH_ENDPOINTS})
        )
        assert assessment.verdict is Verdict.FAIL
        assert 'no integration tests' in assessment.rationale

    def test_uncovered_endpoints_are_listed(self, tmp_path: pathlib.Path):
        test = (
            'def test_deploy(juju):\n'
            '    juju.integrate("my-charm:database", "postgresql-k8s:database")\n'
        )
        assessment = integration_tests.assess(
            _source(
                tmp_path,
                **{
                    'charmcraft.yaml': _CHARMCRAFT_WITH_ENDPOINTS,
                    '.github/workflows/ci.yaml': _INTEGRATION_ON_PUSH,
                    'tests/integration/test_charm.py': test,
                },
            )
        )
        assert assessment.verdict is Verdict.FAIL
        assert 'logging, metrics-endpoint' in assessment.rationale

    def test_another_applications_endpoint_is_not_this_charms_coverage(
        self, tmp_path: pathlib.Path
    ):
        """`postgresql-k8s:database` is the other side of the integration."""
        test = (
            'def test_deploy(juju):\n'
            '    juju.integrate("postgresql-k8s:database", "pgbouncer-k8s:backend-database")\n'
        )
        assessment = integration_tests.assess(
            _source(
                tmp_path,
                **{
                    'charmcraft.yaml': _CHARMCRAFT_WITH_ENDPOINTS,
                    '.github/workflows/ci.yaml': _INTEGRATION_ON_PUSH,
                    'tests/integration/test_charm.py': test,
                },
            )
        )
        assert assessment.verdict is Verdict.FAIL
        assert 'database' in assessment.rationale

    def test_full_coverage_leaves_only_the_undecidable_tail(self, tmp_path: pathlib.Path):
        test = (
            'APP = "my-charm"\n'
            'def test_deploy(juju):\n'
            '    juju.integrate(f"{APP}:database", "postgresql-k8s:database")\n'
            '    juju.integrate(f"{APP}:logging", "loki-k8s:logging")\n'
            '    juju.integrate("my-charm:metrics-endpoint", "prometheus-k8s:metrics-endpoint")\n'
        )
        assessment = integration_tests.assess(
            _source(
                tmp_path,
                **{
                    'charmcraft.yaml': _CHARMCRAFT_WITH_ENDPOINTS,
                    '.github/workflows/ci.yaml': _INTEGRATION_ON_PUSH,
                    'tests/integration/test_charm.py': test,
                },
            )
        )
        assert assessment.verdict is Verdict.NEEDS_HUMAN
        assert 'whether the runs are passing' in assessment.rationale

    def test_tracing_endpoints_are_excluded(self, tmp_path: pathlib.Path):
        charmcraft = (
            'name: my-charm\nrequires:\n'
            '  charm-tracing:\n    interface: tracing\n'
            '  database:\n    interface: postgresql_client\n'
        )
        test = (
            'APP = "my-charm"\n'
            'def test_deploy(juju):\n'
            '    juju.integrate(f"{APP}:database", "postgresql-k8s:database")\n'
        )
        assessment = integration_tests.assess(
            _source(
                tmp_path,
                **{
                    'charmcraft.yaml': charmcraft,
                    '.github/workflows/ci.yaml': _INTEGRATION_ON_PUSH,
                    'tests/integration/test_charm.py': test,
                },
            )
        )
        assert assessment.verdict is Verdict.NEEDS_HUMAN

    def test_tests_that_never_run_on_the_default_branch_fail(self, tmp_path: pathlib.Path):
        workflow = _INTEGRATION_ON_PUSH.replace(
            'push:\n    branches: [main]', 'workflow_dispatch:'
        )
        assessment = integration_tests.assess(
            _source(
                tmp_path,
                **{
                    'charmcraft.yaml': 'name: my-charm\n',
                    '.github/workflows/ci.yaml': workflow,
                    'tests/integration/test_charm.py': 'def test_deploy(juju): pass\n',
                },
            )
        )
        assert assessment.verdict is Verdict.FAIL
        assert 'not run on changes to the default branch' in assessment.rationale

    def test_an_undecidable_clause_does_not_mask_a_decided_one(self, tmp_path: pathlib.Path):
        """Opaque CI is not knowable; uncovered endpoints are, and must be reported."""
        opaque = """
on:
  push:
    branches: [main]
jobs:
  ci:
    uses: canonical/some-actions/.github/workflows/everything.yaml@v1
"""
        assessment = integration_tests.assess(
            _source(
                tmp_path,
                **{
                    'charmcraft.yaml': _CHARMCRAFT_WITH_ENDPOINTS,
                    '.github/workflows/ci.yaml': opaque,
                    'tests/integration/test_charm.py': 'def test_deploy(juju): pass\n',
                },
            )
        )
        assert assessment.verdict is Verdict.FAIL
        assert 'never integrated' in assessment.rationale

    def test_pytest_operator_suites_wire_relations_with_add_relation(self, tmp_path: pathlib.Path):
        """`ops_test.model.add_relation` is still the more common spelling."""
        test = (
            'async def test_deploy(ops_test):\n'
            '    await ops_test.model.add_relation("my-charm:database", "postgresql-k8s")\n'
            '    await ops_test.model.add_relation("my-charm:logging", "loki-k8s")\n'
            '    await ops_test.model.add_relation("my-charm:metrics-endpoint", "prometheus")\n'
        )
        assessment = integration_tests.assess(
            _source(
                tmp_path,
                **{
                    'charmcraft.yaml': _CHARMCRAFT_WITH_ENDPOINTS,
                    '.github/workflows/ci.yaml': _INTEGRATION_ON_PUSH,
                    'tests/integration/test_charm.py': test,
                },
            )
        )
        assert assessment.verdict is Verdict.NEEDS_HUMAN
        assert 'covering all 3 endpoint(s)' in assessment.rationale

    def test_the_charm_is_recognised_without_its_packaging_suffix(self, tmp_path: pathlib.Path):
        """`grafana-k8s` is deployed, and written in tests, as `grafana`."""
        charmcraft = _CHARMCRAFT_WITH_ENDPOINTS.replace('name: my-charm', 'name: my-charm-k8s')
        test = (
            'def test_deploy(juju):\n'
            '    juju.integrate("my-charm:database", "postgresql-k8s:database")\n'
        )
        assessment = integration_tests.assess(
            _source(
                tmp_path,
                'my-charm-k8s',
                **{
                    'charmcraft.yaml': charmcraft,
                    '.github/workflows/ci.yaml': _INTEGRATION_ON_PUSH,
                    'tests/integration/test_charm.py': test,
                },
            )
        )
        assert 'database' not in assessment.rationale

    def test_unreadable_calls_stop_uncovered_being_a_failure(self, tmp_path: pathlib.Path):
        """ "Never integrated" is only true if every call could be read."""
        test = (
            'def test_deploy(juju, endpoint):\n'
            '    juju.integrate(f"my-charm:{endpoint}", "other")\n'
        )
        assessment = integration_tests.assess(
            _source(
                tmp_path,
                **{
                    'charmcraft.yaml': _CHARMCRAFT_WITH_ENDPOINTS,
                    '.github/workflows/ci.yaml': _INTEGRATION_ON_PUSH,
                    'tests/integration/test_charm.py': test,
                },
            )
        )
        assert assessment.verdict is Verdict.NEEDS_HUMAN
        assert 'were not seen integrated' in assessment.rationale

    def test_registered_under_its_checklist_id(self):
        assert ITEM_CHECKS['ci-integration-tests'] is integration_tests

    def test_an_interpolated_application_defers_rather_than_credits(self, tmp_path: pathlib.Path):
        """`database` is an endpoint name both sides of an integration have."""
        test = (
            'OTHER = "postgresql-k8s"\n'
            'def test_deploy(juju):\n'
            '    juju.integrate(f"{OTHER}:database", "other:db")\n'
        )
        assessment = integration_tests.assess(
            _source(
                tmp_path,
                **{
                    'charmcraft.yaml': _CHARMCRAFT_WITH_ENDPOINTS,
                    '.github/workflows/ci.yaml': _INTEGRATION_ON_PUSH,
                    'tests/integration/test_charm.py': test,
                },
            )
        )
        assert assessment.verdict is Verdict.NEEDS_HUMAN
        assert 'not seen integrated' in assessment.rationale
        assert any('application name is interpolated' in line for line in assessment.evidence)

    def test_a_suite_in_one_file_is_still_a_suite(self, tmp_path: pathlib.Path):
        """A small charm keeps the whole suite in tests/test_integration.py."""
        test = (
            'def test_deploy(juju):\n'
            '    juju.integrate("my-charm:database", "postgresql-k8s:database")\n'
            '    juju.integrate("my-charm:logging", "loki-k8s:logging")\n'
            '    juju.integrate("my-charm:metrics-endpoint", "prometheus-k8s:metrics")\n'
            '    juju.integrate("my-charm:ingress", "traefik-k8s:ingress")\n'
        )
        assessment = integration_tests.assess(
            _source(
                tmp_path,
                **{
                    'charmcraft.yaml': _CHARMCRAFT_WITH_ENDPOINTS,
                    '.github/workflows/ci.yaml': _INTEGRATION_ON_PUSH,
                    'tests/test_integration.py': test,
                },
            )
        )
        assert assessment.verdict is not Verdict.FAIL

    def test_a_suite_the_walk_cannot_see_is_not_an_absent_suite(self, tmp_path: pathlib.Path):
        """A workflow running a suite is evidence it exists, whatever the layout."""
        assessment = integration_tests.assess(
            _source(
                tmp_path,
                **{
                    'charmcraft.yaml': _CHARMCRAFT_WITH_ENDPOINTS,
                    '.github/workflows/ci.yaml': _INTEGRATION_ON_PUSH,
                    'tests/spread/task.yaml': 'summary: integration\n',
                },
            )
        )
        assert assessment.verdict is Verdict.NEEDS_HUMAN
        assert 'somewhere this check does not look' in assessment.rationale

    def test_an_unreadable_workflow_defers_the_trigger_clause(self, tmp_path: pathlib.Path):
        test = (
            'def test_deploy(juju):\n'
            '    juju.integrate("my-charm:database", "postgresql-k8s:database")\n'
            '    juju.integrate("my-charm:logging", "loki-k8s:logging")\n'
            '    juju.integrate("my-charm:metrics-endpoint", "prometheus-k8s:metrics")\n'
            '    juju.integrate("my-charm:ingress", "traefik-k8s:ingress")\n'
        )
        assessment = integration_tests.assess(
            _source(
                tmp_path,
                **{
                    'charmcraft.yaml': _CHARMCRAFT_WITH_ENDPOINTS,
                    '.github/workflows/ci.yaml': 'name: [\n',
                    'tests/integration/test_charm.py': test,
                },
            )
        )
        assert assessment.verdict is Verdict.NEEDS_HUMAN
        assert 'could not be read' in assessment.rationale


class TestNoDuplicateModelConfig:
    def test_no_options_does_not_apply(self, tmp_path: pathlib.Path):
        assessment = no_duplicate_model_config.assess(
            _source(tmp_path, **{'charmcraft.yaml': 'name: my-charm\n'})
        )
        assert assessment.verdict is Verdict.NOT_APPLICABLE

    def test_underscored_options_still_match_hyphenated_keys(self, tmp_path: pathlib.Path):
        charmcraft = (
            'name: my-charm\nconfig:\n  options:\n'
            '    http_proxy:\n      type: string\n'
            '    https_proxy:\n      type: string\n'
        )
        assessment = no_duplicate_model_config.assess(
            _source(tmp_path, **{'charmcraft.yaml': charmcraft})
        )
        assert assessment.verdict is Verdict.FAIL
        assert 'http_proxy, https_proxy' in assessment.rationale

    def test_unrelated_options_leave_the_renamed_case_to_a_human(self, tmp_path: pathlib.Path):
        charmcraft = (
            'name: my-charm\nconfig:\n  options:\n'
            '    port:\n      type: int\n      description: The port to listen on.\n'
        )
        assessment = no_duplicate_model_config.assess(
            _source(tmp_path, **{'charmcraft.yaml': charmcraft})
        )
        assert assessment.verdict is Verdict.NEEDS_HUMAN
        assert assessment.evidence == ['port: The port to listen on.']

    def test_registered_under_its_checklist_id(self):
        assert ITEM_CHECKS['best-practice-no-duplicate-model-config'] is no_duplicate_model_config


class TestCharmSource:
    def test_the_repository_root_defaults_to_the_charm_directory(self, tmp_path: pathlib.Path):
        source = CharmSource(charm_path=tmp_path)
        assert source.repo == tmp_path

    def test_a_monorepo_keeps_workflows_above_the_charm(self, tmp_path: pathlib.Path):
        """The charm is in a subdirectory; `.github` stays at the repository root."""
        _charm(
            tmp_path,
            **{
                '.github/workflows/release.yaml': _RELEASE_ON_PUSH,
                'charms/my-charm/charmcraft.yaml': 'name: my-charm\n',
            },
        )
        source = CharmSource(
            charm_path=tmp_path / 'charms' / 'my-charm',
            repo_path=tmp_path,
            charm_name='my-charm',
        )
        assert automated_releasing.assess(source).verdict is Verdict.PASS
        # Without the repository root, the workflows are invisible.
        charm_only = CharmSource(charm_path=tmp_path / 'charms' / 'my-charm')
        assert automated_releasing.assess(charm_only).verdict is Verdict.FAIL


class TestActionAdditionalProperties:
    def test_no_actions_is_not_applicable(self, tmp_path: pathlib.Path):
        assessment = action_additional_properties.assess(
            _source(tmp_path, **{'charmcraft.yaml': 'name: my-charm\n'})
        )
        assert assessment.verdict is Verdict.NOT_APPLICABLE

    def test_missing_additional_properties_fails(self, tmp_path: pathlib.Path):
        charmcraft = (
            'name: my-charm\nactions:\n'
            '  snapshot:\n    description: Take a snapshot.\n'
            '  restore:\n    description: Restore a snapshot.\n    additionalProperties: false\n'
        )
        assessment = action_additional_properties.assess(
            _source(tmp_path, **{'charmcraft.yaml': charmcraft})
        )
        assert assessment.verdict is Verdict.FAIL
        assert 'snapshot' in assessment.rationale
        assert 'restore' not in assessment.rationale

    def test_all_actions_declare_it_passes(self, tmp_path: pathlib.Path):
        charmcraft = (
            'name: my-charm\nactions:\n'
            '  snapshot:\n    description: Take a snapshot.\n    additionalProperties: false\n'
        )
        assessment = action_additional_properties.assess(
            _source(tmp_path, **{'charmcraft.yaml': charmcraft})
        )
        assert assessment.verdict is Verdict.PASS

    def test_registered_under_its_checklist_id(self):
        assert (
            ITEM_CHECKS['charmcraft-actions-additional-properties'] is action_additional_properties
        )


class TestDependencyUpdateTooling:
    def test_no_config_fails(self, tmp_path: pathlib.Path):
        assessment = dependency_update_tooling.assess(_source(tmp_path))
        assert assessment.verdict is Verdict.FAIL

    def test_dependabot_config_passes(self, tmp_path: pathlib.Path):
        assessment = dependency_update_tooling.assess(
            _source(tmp_path, **{'.github/dependabot.yml': 'version: 2\n'})
        )
        assert assessment.verdict is Verdict.PASS

    @pytest.mark.parametrize(
        'path',
        [
            'renovate.json',
            'renovate.json5',
            '.github/renovate.json',
            '.github/renovate.json5',
            '.gitlab/renovate.json',
            '.renovaterc',
            '.renovaterc.json',
        ],
    )
    def test_renovate_config_at_any_recognised_location_passes(
        self, tmp_path: pathlib.Path, path: str
    ):
        assessment = dependency_update_tooling.assess(_source(tmp_path, **{path: '{}\n'}))
        assert assessment.verdict is Verdict.PASS

    def test_renovate_section_in_package_json_passes(self, tmp_path: pathlib.Path):
        assessment = dependency_update_tooling.assess(
            _source(tmp_path, **{'package.json': '{"name": "x", "renovate": {}}\n'})
        )
        assert assessment.verdict is Verdict.PASS

    def test_package_json_without_a_renovate_section_does_not_count(self, tmp_path: pathlib.Path):
        assessment = dependency_update_tooling.assess(
            _source(tmp_path, **{'package.json': '{"name": "x"}\n'})
        )
        assert assessment.verdict is Verdict.FAIL

    def test_monorepo_reads_the_repository_root_not_the_charm_directory(
        self, tmp_path: pathlib.Path
    ):
        _charm(
            tmp_path,
            **{
                '.github/dependabot.yml': 'version: 2\n',
                'charms/my-charm/charmcraft.yaml': 'name: my-charm\n',
            },
        )
        source = CharmSource(charm_path=tmp_path / 'charms' / 'my-charm', repo_path=tmp_path)
        assert dependency_update_tooling.assess(source).verdict is Verdict.PASS
        charm_only = CharmSource(charm_path=tmp_path / 'charms' / 'my-charm')
        assert dependency_update_tooling.assess(charm_only).verdict is Verdict.FAIL

    def test_registered_under_its_checklist_id(self):
        assert (
            ITEM_CHECKS['best-practice-automated-dependency-updates'] is dependency_update_tooling
        )


class TestAvoidCharmPlugin:
    def test_no_parts_declared_fails(self, tmp_path: pathlib.Path):
        assessment = avoid_charm_plugin.assess(
            _source(tmp_path, **{'charmcraft.yaml': 'name: my-charm\n'})
        )
        assert assessment.verdict is Verdict.FAIL
        assert 'default `charm` plugin' in assessment.rationale

    def test_explicit_charm_plugin_fails(self, tmp_path: pathlib.Path):
        charmcraft = 'name: my-charm\nparts:\n  charm:\n    plugin: charm\n'
        assessment = avoid_charm_plugin.assess(
            _source(tmp_path, **{'charmcraft.yaml': charmcraft})
        )
        assert assessment.verdict is Verdict.FAIL

    def test_charm_plugin_inferred_from_the_part_name_fails(self, tmp_path: pathlib.Path):
        """No `plugin` key: charmcraft infers it from the part's own name."""
        charmcraft = 'name: my-charm\nparts:\n  charm:\n    source: .\n'
        assessment = avoid_charm_plugin.assess(
            _source(tmp_path, **{'charmcraft.yaml': charmcraft})
        )
        assert assessment.verdict is Verdict.FAIL

    @pytest.mark.parametrize('plugin', ['uv', 'poetry'])
    def test_migrated_plugin_passes(self, tmp_path: pathlib.Path, plugin: str):
        charmcraft = f'name: my-charm\nparts:\n  charm:\n    plugin: {plugin}\n'
        assessment = avoid_charm_plugin.assess(
            _source(tmp_path, **{'charmcraft.yaml': charmcraft})
        )
        assert assessment.verdict is Verdict.PASS

    def test_a_part_named_after_a_migrated_plugin_passes(self, tmp_path: pathlib.Path):
        """A part with no `plugin` key named `uv` is inferred as the `uv` plugin."""
        charmcraft = 'name: my-charm\nparts:\n  uv:\n    source: .\n'
        assessment = avoid_charm_plugin.assess(
            _source(tmp_path, **{'charmcraft.yaml': charmcraft})
        )
        assert assessment.verdict is Verdict.PASS

    def test_unrecognised_plugin_name_needs_a_human(self, tmp_path: pathlib.Path):
        charmcraft = 'name: my-charm\nparts:\n  static-files:\n    plugin: dump\n'
        assessment = avoid_charm_plugin.assess(
            _source(tmp_path, **{'charmcraft.yaml': charmcraft})
        )
        assert assessment.verdict is Verdict.NEEDS_HUMAN

    def test_a_charm_plugin_part_fails_even_alongside_a_migrated_one(self, tmp_path: pathlib.Path):
        """Mid-migration counts as still using the plugin the item says to avoid."""
        charmcraft = (
            'name: my-charm\nparts:\n  charm:\n    plugin: charm\n  extra:\n    plugin: uv\n'
        )
        assessment = avoid_charm_plugin.assess(
            _source(tmp_path, **{'charmcraft.yaml': charmcraft})
        )
        assert assessment.verdict is Verdict.FAIL

    def test_registered_under_its_checklist_id(self):
        assert ITEM_CHECKS['best-practice-avoid-charm-plugin'] is avoid_charm_plugin
