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
    FixtureFetcher,
    ItemCheck,
    automated_releasing,
    avoid_charm_plugin,
    config_defaults,
    dependency_update_tooling,
    documentation_link,
    first_party_python_files,
    integration_tests,
    library_status_mutation,
    logging_not_print,
    no_duplicate_model_config,
    pin_workload_versions,
    resource_architectures,
    safe_subprocess,
    sensitive_logging,
    summary_description,
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

    def test_excludes_github_and_docs_tooling(self, tmp_path: pathlib.Path):
        """CI/doc-build tooling the author wrote, but Juju never runs.

        Found by running item 10 against real charms: `charm-ubuntu`'s
        `.github/check-conventional-pr-title.py` and `zookeeper-operator`'s
        `docs/.sphinx/get_vale_conf.py` both surfaced print() findings that
        have nothing to do with the charm's own logging behaviour.
        """
        charm = _charm(
            tmp_path,
            **{
                'src/charm.py': '',
                '.github/check-pr-title.py': '',
                'docs/.sphinx/get_vale_conf.py': '',
            },
        )
        found = [p.relative_to(charm).as_posix() for p in first_party_python_files(charm)]
        assert found == ['src/charm.py']

    def test_includes_scripts_deployed_onto_the_workload(self, tmp_path: pathlib.Path):
        """`scripts/` is charm runtime behaviour under a different name.

        postgresql-operator deploys `scripts/rotate_logs.py` onto the target
        machine and runs it there - unlike `.github`/`docs`, this is not
        build-time tooling.
        """
        charm = _charm(tmp_path, **{'src/charm.py': '', 'scripts/rotate_logs.py': ''})
        found = [p.relative_to(charm).as_posix() for p in first_party_python_files(charm)]
        assert found == ['scripts/rotate_logs.py', 'src/charm.py']


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


_K8S_CHARMCRAFT = """
name: my-charm
containers:
  workload:
    resource: my-image
resources:
  my-image:
    type: oci-image
    description: OCI image
    upstream-source: {reference}
"""


def _k8s_source(tmp_path: pathlib.Path, reference: str | None) -> CharmSource:
    charmcraft = _K8S_CHARMCRAFT.format(reference=reference or '')
    if reference is None:
        # No `upstream-source` key at all, not merely an empty value.
        charmcraft = 'name: my-charm\ncontainers:\n  workload:\n    resource: my-image\n'
        charmcraft += 'resources:\n  my-image:\n    type: oci-image\n    description: OCI image\n'
    return _source(tmp_path, **{'charmcraft.yaml': charmcraft})


class TestPinWorkloadVersionsKubernetes:
    def test_digest_pin_passes(self, tmp_path: pathlib.Path):
        reference = 'docker.io/ubuntu/grafana@sha256:' + 'a' * 64
        assessment = pin_workload_versions.assess(_k8s_source(tmp_path, reference))
        assert assessment.verdict is Verdict.PASS
        assert 'digest' in assessment.rationale

    def test_specific_tag_passes(self, tmp_path: pathlib.Path):
        assessment = pin_workload_versions.assess(
            _k8s_source(tmp_path, 'ghcr.io/forgejo/forgejo:9.0.3-rootless')
        )
        assert assessment.verdict is Verdict.PASS
        assert 'version tag' in assessment.rationale

    def test_latest_tag_fails(self, tmp_path: pathlib.Path):
        assessment = pin_workload_versions.assess(
            _k8s_source(tmp_path, 'ghcr.io/forgejo/forgejo:latest')
        )
        assert assessment.verdict is Verdict.FAIL
        assert 'my-image' in assessment.rationale

    def test_no_tag_fails(self, tmp_path: pathlib.Path):
        assessment = pin_workload_versions.assess(_k8s_source(tmp_path, 'ghcr.io/forgejo/forgejo'))
        assert assessment.verdict is Verdict.FAIL

    def test_missing_upstream_source_needs_a_human(self, tmp_path: pathlib.Path):
        assessment = pin_workload_versions.assess(_k8s_source(tmp_path, None))
        assert assessment.verdict is Verdict.NEEDS_HUMAN
        assert 'my-image' in assessment.rationale

    def test_registry_port_is_not_mistaken_for_a_tag(self, tmp_path: pathlib.Path):
        """`host:5000/image` has a colon that is a port, not a floating tag."""
        assessment = pin_workload_versions.assess(
            _k8s_source(tmp_path, 'myregistry:5000/super-app@sha256:' + 'b' * 64)
        )
        assert assessment.verdict is Verdict.PASS

    def test_one_floating_resource_fails_even_if_another_is_pinned(self, tmp_path: pathlib.Path):
        charmcraft = (
            'name: my-charm\n'
            'containers:\n  workload:\n    resource: pinned-image\n  sidecar:\n'
            '    resource: floating-image\n'
            'resources:\n'
            '  pinned-image:\n    type: oci-image\n    description: pinned\n'
            '    upstream-source: docker.io/foo/pinned@sha256:' + 'c' * 64 + '\n'
            '  floating-image:\n    type: oci-image\n    description: floating\n'
            '    upstream-source: docker.io/foo/floating:latest\n'
        )
        assessment = pin_workload_versions.assess(
            _source(tmp_path, **{'charmcraft.yaml': charmcraft})
        )
        assert assessment.verdict is Verdict.FAIL
        assert 'floating-image' in assessment.rationale

    def test_kubernetes_charm_with_no_oci_resource_is_not_applicable(self, tmp_path: pathlib.Path):
        charmcraft = 'name: my-charm\ncontainers:\n  workload:\n    resource: my-image\n'
        assessment = pin_workload_versions.assess(
            _source(tmp_path, **{'charmcraft.yaml': charmcraft})
        )
        assert assessment.verdict is Verdict.NOT_APPLICABLE


class TestPinWorkloadVersionsMachine:
    def test_no_install_logic_is_not_applicable(self, tmp_path: pathlib.Path):
        assessment = pin_workload_versions.assess(
            _source(tmp_path, **{'src/charm.py': 'import ops\n'})
        )
        assert assessment.verdict is Verdict.NOT_APPLICABLE

    def test_apt_lib_pinned_version_passes(self, tmp_path: pathlib.Path):
        assessment = pin_workload_versions.assess(
            _source(
                tmp_path,
                **{
                    'src/charm.py': (
                        'from charms.operator_libs_linux.v0 import apt\n'
                        "apt.add_package('nginx', version='1.18.0-0ubuntu1')\n"
                    )
                },
            )
        )
        assert assessment.verdict is Verdict.PASS

    def test_apt_lib_no_version_fails(self, tmp_path: pathlib.Path):
        """Mirrors hardware-observer-operator's `apt.add_package(self.pkg, update_cache=True)`."""
        assessment = pin_workload_versions.assess(
            _source(
                tmp_path,
                **{
                    'src/charm.py': (
                        'from charms.operator_libs_linux.v0 import apt\n'
                        "apt.add_package('freeipmi-tools', update_cache=True)\n"
                    )
                },
            )
        )
        assert assessment.verdict is Verdict.FAIL
        assert 'no `version`' in assessment.rationale or 'do not pin' in assessment.rationale

    def test_apt_lib_computed_version_needs_a_human(self, tmp_path: pathlib.Path):
        """Mirrors hardware-observer-operator's ``add_pkg_with_candidate_version``.

        ``apt.add_package(pkg, version=version)`` looks pinned at the call
        site, but the real charm resolves ``version`` from ``apt-cache
        policy``'s candidate - i.e. "whatever is newest" - which this check
        cannot tell apart from a real pin without reading `get_candidate_version`.
        """
        assessment = pin_workload_versions.assess(
            _source(
                tmp_path,
                **{
                    'src/apt_helpers.py': (
                        'from charms.operator_libs_linux.v0 import apt\n'
                        'def add_pkg_with_candidate_version(pkg):\n'
                        '    version = get_candidate_version(pkg)\n'
                        '    apt.add_package(pkg, version=version, update_cache=False)\n'
                    )
                },
            )
        )
        assert assessment.verdict is Verdict.NEEDS_HUMAN

    def test_snap_ensure_with_revision_passes(self, tmp_path: pathlib.Path):
        """Mirrors postgresql-operator's ``snap_package.ensure(..., revision=..., channel)``."""
        assessment = pin_workload_versions.assess(
            _source(
                tmp_path,
                **{
                    'src/charm.py': (
                        'def install(snap_package, revision, channel):\n'
                        '    snap_package.ensure(\n'
                        '        SnapState.Latest, revision=revision, channel=channel\n'
                        '    )\n'
                    )
                },
            )
        )
        assert assessment.verdict is Verdict.PASS

    def test_snap_add_channel_only_fails(self, tmp_path: pathlib.Path):
        """Mirrors hardware-observer-operator's ``snap.add(name, channel=self.channel)``."""
        assessment = pin_workload_versions.assess(
            _source(
                tmp_path,
                **{
                    'src/charm.py': (
                        'from charms.operator_libs_linux.v2 import snap\n'
                        'snap.add(self.snap_name, channel=self.channel)\n'
                    )
                },
            )
        )
        assert assessment.verdict is Verdict.FAIL
        assert 'channel' in assessment.rationale or 'do not pin' in assessment.rationale

    def test_unrelated_ensure_call_not_mistaken_for_snap_install(self, tmp_path: pathlib.Path):
        """A bare ``.ensure()`` with no channel/revision keyword is too generic to match."""
        assessment = pin_workload_versions.assess(
            _source(tmp_path, **{'src/charm.py': 'thing.ensure()\n'})
        )
        assert assessment.verdict is Verdict.NOT_APPLICABLE

    @pytest.mark.parametrize(
        'source,expected',
        [
            ("subprocess.run(['apt-get', 'install', '-y', 'mysql-server'])", Verdict.FAIL),
            ("subprocess.run(['apt-get', 'install', '-y', 'mysql-server=8.0.35'])", Verdict.PASS),
            (
                "subprocess.run(['snap', 'install', 'foo', '--channel=stable'])",
                Verdict.FAIL,
            ),
            (
                "subprocess.run(['snap', 'install', 'foo', '--revision=123'])",
                Verdict.PASS,
            ),
            ("subprocess.run(['pip', 'install', 'requests'])", Verdict.FAIL),
            ("subprocess.run(['pip', 'install', 'requests==2.31.0'])", Verdict.PASS),
            (
                "subprocess.run(['curl', '-LO', 'https://example.com/foo/latest/foo.tar.gz'])",
                Verdict.FAIL,
            ),
        ],
    )
    def test_cli_install_commands(self, tmp_path: pathlib.Path, source: str, expected: Verdict):
        assessment = pin_workload_versions.assess(
            _source(tmp_path, **{'src/charm.py': f'import subprocess\n{source}\n'})
        )
        assert assessment.verdict is expected

    def test_cli_download_without_a_version_marker_needs_a_human(self, tmp_path: pathlib.Path):
        assessment = pin_workload_versions.assess(
            _source(
                tmp_path,
                **{
                    'src/charm.py': (
                        'import subprocess\n'
                        "subprocess.run(['curl', '-LO', 'https://example.com/foo/foo.tar.gz'])\n"
                    )
                },
            )
        )
        assert assessment.verdict is Verdict.NEEDS_HUMAN

    def test_program_name_literal_but_arguments_built_elsewhere_needs_a_human(
        self, tmp_path: pathlib.Path
    ):
        assessment = pin_workload_versions.assess(
            _source(
                tmp_path,
                **{
                    'src/charm.py': (
                        'import subprocess\n'
                        'def install(args):\n'
                        "    subprocess.run(['apt-get', *args])\n"
                    )
                },
            )
        )
        assert assessment.verdict is Verdict.NEEDS_HUMAN

    def test_unparseable_source_needs_a_human(self, tmp_path: pathlib.Path):
        assessment = pin_workload_versions.assess(
            _source(tmp_path, **{'src/charm.py': 'def broken(:\n'})
        )
        assert assessment.verdict is Verdict.NEEDS_HUMAN

    def test_registered_under_its_checklist_id(self):
        assert ITEM_CHECKS['best-practice-pin-workload-versions'] is pin_workload_versions


_LONG_DESCRIPTION = (
    'A Juju charm that deploys and manages the Example workload on Kubernetes. It provides a '
    'web interface for exploring data, and integrates with several other charms over Juju '
    'relations. Configuration options let you tune resource limits, enable TLS, and control '
    'log verbosity. Deviations from the unpackaged application: the charm always runs as a '
    'non-root user and disables the built-in scheduler in favour of Juju.'
)


class TestSummaryDescription:
    def test_paraphrased_description_fails(self, tmp_path: pathlib.Path):
        """The real forgejo-k8s example: description barely longer than summary."""
        charmcraft = (
            'name: my-charm\n'
            'summary: Charmed Kubernetes operator for Forgejo.\n'
            'description: Deploy and configure the software forge, Forgejo.\n'
        )
        assessment = summary_description.assess(
            _source(tmp_path, **{'charmcraft.yaml': charmcraft})
        )
        assert assessment.verdict is Verdict.FAIL
        assert 'paraphrase' in assessment.rationale

    def test_much_longer_description_needs_a_human(self, tmp_path: pathlib.Path):
        charmcraft = (
            'name: my-charm\n'
            'summary: Data visualization and observability with Example.\n'
            f'description: |\n  {_LONG_DESCRIPTION}\n'
        )
        assessment = summary_description.assess(
            _source(tmp_path, **{'charmcraft.yaml': charmcraft})
        )
        assert assessment.verdict is Verdict.NEEDS_HUMAN

    def test_short_but_unrelated_description_needs_a_human(self, tmp_path: pathlib.Path):
        """Short and low-overlap: not a confident paraphrase, not a guessed pass either."""
        charmcraft = (
            'name: my-charm\n'
            'summary: A pristine Ubuntu Server\n'
            'description: This simply deploys the Ubuntu Cloud/Server image\n'
        )
        assessment = summary_description.assess(
            _source(tmp_path, **{'charmcraft.yaml': charmcraft})
        )
        assert assessment.verdict is Verdict.NEEDS_HUMAN

    def test_missing_description_needs_a_human(self, tmp_path: pathlib.Path):
        assessment = summary_description.assess(
            _source(tmp_path, **{'charmcraft.yaml': 'name: my-charm\nsummary: Foo.\n'})
        )
        assert assessment.verdict is Verdict.NEEDS_HUMAN

    def test_registered_under_its_checklist_id(self):
        assert ITEM_CHECKS['charmcraft-summary-description'] is summary_description


class TestLoggingNotPrint:
    def test_no_print_no_commands_passes(self, tmp_path: pathlib.Path):
        assessment = logging_not_print.assess(
            _source(tmp_path, **{'src/charm.py': 'import ops\nlogger.info("hi")\n'})
        )
        assert assessment.verdict is Verdict.PASS

    def test_print_call_fails(self, tmp_path: pathlib.Path):
        assessment = logging_not_print.assess(
            _source(tmp_path, **{'src/charm.py': 'print("debugging")\n'})
        )
        assert assessment.verdict is Verdict.FAIL
        assert 'print()' in assessment.rationale

    def test_captured_container_exec_passes(self, tmp_path: pathlib.Path):
        assessment = logging_not_print.assess(
            _source(
                tmp_path,
                **{
                    'src/charm.py': (
                        "import ops\noutput, _ = container.exec(['/usr/bin/id']).wait_output()\n"
                    )
                },
            )
        )
        assert assessment.verdict is Verdict.PASS

    def test_print_outranks_undecidable_capture(self, tmp_path: pathlib.Path):
        """A settled failure is reported even when another site is unclear."""
        assessment = logging_not_print.assess(
            _source(
                tmp_path,
                **{
                    'src/charm.py': (
                        'import ops\n'
                        "print('debug')\n"
                        "proc = container.exec(['/usr/bin/id'])\n"
                        'proc.wait_output()\n'
                    )
                },
            )
        )
        assert assessment.verdict is Verdict.FAIL

    def test_findings_in_vendored_libs_are_not_the_charms_problem(self, tmp_path: pathlib.Path):
        assessment = logging_not_print.assess(
            _source(
                tmp_path,
                **{
                    'src/charm.py': 'import ops\n',
                    'lib/charms/someone_else/v0/their_lib.py': 'print("not our problem")\n',
                },
            )
        )
        assert assessment.verdict is Verdict.PASS

    def test_registered_under_its_checklist_id(self):
        assert ITEM_CHECKS['best-practice-use-logging-not-print'] is logging_not_print


class TestSensitiveLogging:
    def test_no_logging_calls_is_not_applicable(self, tmp_path: pathlib.Path):
        assessment = sensitive_logging.assess(
            _source(tmp_path, **{'src/charm.py': 'import ops\n'})
        )
        assert assessment.verdict is Verdict.NOT_APPLICABLE

    def test_logging_a_fetch_relation_data_result_fails(self, tmp_path: pathlib.Path):
        """The real forgejo-k8s finding: a databag logged right before reading a password."""
        assessment = sensitive_logging.assess(
            _source(
                tmp_path,
                **{
                    'src/charm.py': (
                        'relations = self.database.fetch_relation_data()\n'
                        'logger.debug("Got following database data: %s", relations)\n'
                    )
                },
            )
        )
        assert assessment.verdict is Verdict.FAIL
        assert any('fetch_relation_data' in line for line in assessment.evidence)

    def test_logging_a_relation_databag_subscript_fails(self, tmp_path: pathlib.Path):
        assessment = sensitive_logging.assess(
            _source(
                tmp_path,
                **{
                    'src/charm.py': (
                        'bag = relation.data[self.unit]\nlogger.debug("unit data: %s", bag)\n'
                    )
                },
            )
        )
        assert assessment.verdict is Verdict.FAIL

    def test_logging_self_config_fails(self, tmp_path: pathlib.Path):
        assessment = sensitive_logging.assess(
            _source(tmp_path, **{'src/charm.py': 'logger.debug("config: %s", self.config)\n'})
        )
        assert assessment.verdict is Verdict.FAIL
        assert 'self.config' in assessment.rationale

    def test_logging_os_environ_fails(self, tmp_path: pathlib.Path):
        assessment = sensitive_logging.assess(
            _source(
                tmp_path,
                **{'src/charm.py': 'import os\nlogger.debug("env: %s", os.environ)\n'},
            )
        )
        assert assessment.verdict is Verdict.FAIL
        assert 'os.environ' in assessment.rationale

    def test_logging_one_field_is_not_caught(self, tmp_path: pathlib.Path):
        """Real forgejo-k8s code today: logs one extracted field, not the whole databag."""
        assessment = sensitive_logging.assess(
            _source(
                tmp_path,
                **{
                    'src/charm.py': (
                        'for data in relations.values():\n'
                        '    logger.info("New database endpoint is %s", data["endpoints"])\n'
                    )
                },
            )
        )
        assert assessment.verdict is Verdict.PASS

    def test_logging_calls_with_no_sensitive_shape_passes(self, tmp_path: pathlib.Path):
        assessment = sensitive_logging.assess(
            _source(tmp_path, **{'src/charm.py': 'logger.info("Reconciling workload state")\n'})
        )
        assert assessment.verdict is Verdict.PASS

    def test_unparsed_file_with_no_finding_needs_a_human(self, tmp_path: pathlib.Path):
        assessment = sensitive_logging.assess(
            _source(tmp_path, **{'src/charm.py': 'def broken(:\n'})
        )
        assert assessment.verdict is Verdict.NEEDS_HUMAN

    def test_findings_in_vendored_libs_are_not_the_charms_problem(self, tmp_path: pathlib.Path):
        assessment = sensitive_logging.assess(
            _source(
                tmp_path,
                **{
                    'src/charm.py': 'import ops\n',
                    'lib/charms/someone_else/v0/their_lib.py': (
                        'logger.debug("dump: %s", self.config)\n'
                    ),
                },
            )
        )
        assert assessment.verdict is Verdict.NOT_APPLICABLE

    def test_registered_under_its_checklist_id(self):
        assert ITEM_CHECKS['best-practice-no-sensitive-data-in-logs'] is sensitive_logging


class TestResourceArchitectures:
    def test_no_resources_is_not_applicable(self, tmp_path: pathlib.Path):
        assessment = resource_architectures.assess(
            _source(tmp_path, **{'charmcraft.yaml': 'name: my-charm\n'})
        )
        assert assessment.verdict is Verdict.NOT_APPLICABLE

    def test_oci_image_only_is_not_applicable(self, tmp_path: pathlib.Path):
        """An OCI reference is item 16's territory, not this item's."""
        charmcraft = (
            'name: my-charm\n'
            'containers:\n  workload:\n    resource: my-image\n'
            'resources:\n  my-image:\n    type: oci-image\n    description: OCI image\n'
        )
        assessment = resource_architectures.assess(
            _source(tmp_path, **{'charmcraft.yaml': charmcraft})
        )
        assert assessment.verdict is Verdict.NOT_APPLICABLE

    def test_file_resource_needs_a_human(self, tmp_path: pathlib.Path):
        charmcraft = (
            'name: my-charm\n'
            'resources:\n'
            '  lxd-binary:\n    type: file\n    filename: lxd\n    description: The LXD binary\n'
            'platforms:\n  ubuntu@24.04:amd64:\n  ubuntu@24.04:arm64:\n'
        )
        assessment = resource_architectures.assess(
            _source(tmp_path, **{'charmcraft.yaml': charmcraft})
        )
        assert assessment.verdict is Verdict.NEEDS_HUMAN
        assert 'lxd-binary' in assessment.rationale
        assert 'amd64' in assessment.rationale
        assert 'arm64' in assessment.rationale

    def test_file_resource_with_bare_architecture_platforms(self, tmp_path: pathlib.Path):
        """A platform key can be a bare architecture, not just `<base>@<version>:<arch>`."""
        charmcraft = (
            'name: my-charm\n'
            'resources:\n'
            '  my-binary:\n    type: file\n    filename: bin\n    description: A binary\n'
            'platforms:\n  amd64:\n'
        )
        assessment = resource_architectures.assess(
            _source(tmp_path, **{'charmcraft.yaml': charmcraft})
        )
        assert assessment.verdict is Verdict.NEEDS_HUMAN
        assert 'amd64' in assessment.rationale

    def test_file_resource_with_no_platforms_needs_a_human(self, tmp_path: pathlib.Path):
        charmcraft = (
            'name: my-charm\n'
            'resources:\n'
            '  my-binary:\n    type: file\n    filename: bin\n    description: A binary\n'
        )
        assessment = resource_architectures.assess(
            _source(tmp_path, **{'charmcraft.yaml': charmcraft})
        )
        assert assessment.verdict is Verdict.NEEDS_HUMAN
        assert 'my-binary' in assessment.rationale

    def test_registered_under_its_checklist_id(self):
        assert ITEM_CHECKS['charmcraft-yaml-key-resources'] is resource_architectures


# The exact URL `gather_resource_architectures_network` builds - not imported from
# `item_checks` (a private constant), just matched literally, the same way these tests
# already hardcode expected rationale text rather than importing it.
_CHARMHUB_URL_TEMPLATE = (
    'https://api.charmhub.io/v2/charms/info/{name}?fields=channel-map.revision.bases.architecture'
)

_CHARMCRAFT_WITH_FILE_RESOURCE = (
    'name: my-charm\n'
    'resources:\n'
    '  lxd-binary:\n    type: file\n    filename: lxd\n    description: The LXD binary\n'
    'platforms:\n  ubuntu@24.04:amd64:\n  ubuntu@24.04:arm64:\n'
)


class _SpyFetcher:
    """Wraps a `Fetcher`, recording every URL it is asked for."""

    def __init__(self, fetcher: FixtureFetcher):
        self._fetcher = fetcher
        self.requested: list[str] = []

    def get_json(self, url: str):
        self.requested.append(url)
        return self._fetcher.get_json(url)

    def get_text(self, url: str):
        self.requested.append(url)
        return self._fetcher.get_text(url)


class TestResourceArchitecturesNetwork:
    def test_no_fetcher_degrades_to_the_local_only_gate(self, tmp_path: pathlib.Path):
        source = _source(tmp_path, **{'charmcraft.yaml': _CHARMCRAFT_WITH_FILE_RESOURCE})
        without_fetcher = resource_architectures.assess(source)
        with_none_fetcher = resource_architectures.assess(source, fetcher=None)
        assert without_fetcher.rationale == with_none_fetcher.rationale
        assert without_fetcher.verdict is Verdict.NEEDS_HUMAN

    def test_skips_the_network_call_when_no_file_resource(self, tmp_path: pathlib.Path):
        source = _source(tmp_path, **{'charmcraft.yaml': 'name: my-charm\n'})
        fetcher = _SpyFetcher(FixtureFetcher())
        assessment = resource_architectures.assess(source, fetcher=fetcher)
        assert assessment.verdict is Verdict.NOT_APPLICABLE
        assert fetcher.requested == []

    def test_charmhub_confirms_every_claimed_architecture(self, tmp_path: pathlib.Path):
        source = _source(tmp_path, **{'charmcraft.yaml': _CHARMCRAFT_WITH_FILE_RESOURCE})
        url = _CHARMHUB_URL_TEMPLATE.format(name='my-charm')
        fetcher = FixtureFetcher({
            url: {
                'channel-map': [
                    {'revision': {'bases': [{'architecture': 'amd64'}]}},
                    {'revision': {'bases': [{'architecture': 'arm64'}]}},
                ]
            }
        })
        assessment = resource_architectures.assess(source, fetcher=fetcher)
        assert assessment.verdict is Verdict.NEEDS_HUMAN
        assert 'published for every claimed architecture' in assessment.rationale
        assert 'amd64' in assessment.rationale
        assert 'arm64' in assessment.rationale

    def test_charmhub_missing_architecture_still_needs_a_human_not_fail(
        self, tmp_path: pathlib.Path
    ):
        """Confirming a gap strengthens the rationale, but PLAN.md's design note is explicit

        that the public API does not unlock a PASS/FAIL branch - it stays NEEDS_HUMAN.
        """
        source = _source(tmp_path, **{'charmcraft.yaml': _CHARMCRAFT_WITH_FILE_RESOURCE})
        url = _CHARMHUB_URL_TEMPLATE.format(name='my-charm')
        fetcher = FixtureFetcher({
            url: {'channel-map': [{'revision': {'bases': [{'architecture': 'amd64'}]}}]}
        })
        assessment = resource_architectures.assess(source, fetcher=fetcher)
        assert assessment.verdict is Verdict.NEEDS_HUMAN
        assert 'not for arm64' in assessment.rationale

    def test_charmhub_lookup_failure_is_needs_human_not_fail(self, tmp_path: pathlib.Path):
        source = _source(tmp_path, **{'charmcraft.yaml': _CHARMCRAFT_WITH_FILE_RESOURCE})
        fetcher = FixtureFetcher()  # empty - every get_json() misses, same as a network error
        assessment = resource_architectures.assess(source, fetcher=fetcher)
        assert assessment.verdict is Verdict.NEEDS_HUMAN
        assert 'did not return a result' in assessment.rationale

    def test_name_resolved_from_charmcraft_yaml_not_the_repository_name(
        self, tmp_path: pathlib.Path
    ):
        """The query name must never come from `CharmSource.charm_name` (repo-derived)."""
        source = CharmSource(
            charm_path=_charm(tmp_path, **{'charmcraft.yaml': _CHARMCRAFT_WITH_FILE_RESOURCE}),
            charm_name='my-charm-operator',
        )
        url = _CHARMHUB_URL_TEMPLATE.format(name='my-charm')
        fetcher = FixtureFetcher({
            url: {'channel-map': [{'revision': {'bases': [{'architecture': 'amd64'}]}}]}
        })
        assessment = resource_architectures.assess(source, fetcher=fetcher)
        assert 'not for arm64' in assessment.rationale

    def test_name_resolved_from_legacy_metadata_yaml_fallback(self, tmp_path: pathlib.Path):
        """A charm that hasn't migrated `name:` into `charmcraft.yaml` yet."""
        charmcraft = (
            'resources:\n'
            '  lxd-binary:\n    type: file\n    filename: lxd\n    description: The LXD binary\n'
            'platforms:\n  ubuntu@24.04:amd64:\n'
        )
        source = _source(
            tmp_path,
            **{'charmcraft.yaml': charmcraft, 'metadata.yaml': 'name: my-legacy-charm\n'},
        )
        url = _CHARMHUB_URL_TEMPLATE.format(name='my-legacy-charm')
        fetcher = FixtureFetcher({
            url: {'channel-map': [{'revision': {'bases': [{'architecture': 'amd64'}]}}]}
        })
        assessment = resource_architectures.assess(source, fetcher=fetcher)
        assert 'published for every claimed architecture' in assessment.rationale

    def test_unresolvable_name_is_needs_human_not_fail(self, tmp_path: pathlib.Path):
        charmcraft = (
            'resources:\n'
            '  lxd-binary:\n    type: file\n    filename: lxd\n    description: The LXD binary\n'
        )
        source = _source(tmp_path, **{'charmcraft.yaml': charmcraft})
        assessment = resource_architectures.assess(source, fetcher=FixtureFetcher())
        assert assessment.verdict is Verdict.NEEDS_HUMAN
        assert 'could not resolve a charm name' in assessment.rationale


class TestLibraryStatusMutation:
    def test_no_own_library_is_not_applicable(self, tmp_path: pathlib.Path):
        assessment = library_status_mutation.assess(
            _source(
                tmp_path,
                **{
                    'src/charm.py': 'import ops\n',
                    'lib/charms/someone_else/v0/their_lib.py': (
                        'self.unit.status = BlockedStatus("bad")\n'
                    ),
                },
            )
        )
        assert assessment.verdict is Verdict.NOT_APPLICABLE

    def test_own_library_setting_unit_status_fails(self, tmp_path: pathlib.Path):
        assessment = library_status_mutation.assess(
            _source(
                tmp_path,
                **{
                    'lib/charms/my_charm/v0/my_lib.py': (
                        'self.unit.status = BlockedStatus("bad")\n'
                    ),
                },
            )
        )
        assert assessment.verdict is Verdict.FAIL
        assert '.unit.status' in assessment.rationale or 'unit or application status' in (
            assessment.rationale
        )

    def test_own_library_setting_app_status_via_model_fails(self, tmp_path: pathlib.Path):
        """The `self.model.app.status = ...` shape rolling_ops uses - a longer chain."""
        assessment = library_status_mutation.assess(
            _source(
                tmp_path,
                **{
                    'lib/charms/my_charm/v0/my_lib.py': (
                        'self.model.app.status = ActiveStatus()\n'
                    ),
                },
            )
        )
        assert assessment.verdict is Verdict.FAIL

    def test_own_library_with_no_mutation_passes(self, tmp_path: pathlib.Path):
        assessment = library_status_mutation.assess(
            _source(
                tmp_path,
                **{
                    'lib/charms/my_charm/v0/my_lib.py': (
                        'def check(self):\n    return self.unit.status\n'
                    ),
                },
            )
        )
        assert assessment.verdict is Verdict.PASS

    def test_status_comparison_is_not_a_mutation(self, tmp_path: pathlib.Path):
        """Reading/comparing status is not setting it - only an assignment target matters."""
        assessment = library_status_mutation.assess(
            _source(
                tmp_path,
                **{
                    'lib/charms/my_charm/v0/my_lib.py': (
                        'if self.unit.status == ActiveStatus():\n    pass\n'
                    ),
                },
            )
        )
        assert assessment.verdict is Verdict.PASS

    def test_unparsed_own_library_file_needs_a_human(self, tmp_path: pathlib.Path):
        assessment = library_status_mutation.assess(
            _source(tmp_path, **{'lib/charms/my_charm/v0/my_lib.py': 'def broken(:\n'})
        )
        assert assessment.verdict is Verdict.NEEDS_HUMAN

    def test_vendored_library_mutation_is_not_the_charms_problem(self, tmp_path: pathlib.Path):
        """The real shape found across this project's vendored charmlibs - not scoped in."""
        assessment = library_status_mutation.assess(
            _source(
                tmp_path,
                **{
                    'lib/charms/data_platform_libs/v0/upgrade.py': (
                        'self.charm.unit.status = BlockedStatus(cause)\n'
                    ),
                },
            )
        )
        assert assessment.verdict is Verdict.NOT_APPLICABLE

    def test_registered_under_its_checklist_id(self):
        assert ITEM_CHECKS['best-practice-libraries-no-status-mutation'] is library_status_mutation


class TestDocumentationLink:
    def test_no_documentation_link_is_not_applicable(self, tmp_path: pathlib.Path):
        charmcraft = 'name: my-charm\nlinks:\n  issues:\n    - https://example.com/issues\n'
        assessment = documentation_link.assess(
            _source(tmp_path, **{'charmcraft.yaml': charmcraft})
        )
        assert assessment.verdict is Verdict.NOT_APPLICABLE

    def test_no_links_block_at_all_is_not_applicable(self, tmp_path: pathlib.Path):
        assessment = documentation_link.assess(
            _source(tmp_path, **{'charmcraft.yaml': 'name: my-charm\n'})
        )
        assert assessment.verdict is Verdict.NOT_APPLICABLE

    def test_documentation_matching_website_domain_fails(self, tmp_path: pathlib.Path):
        """The violation shape the item describes: documentation is just the app's own site."""
        charmcraft = (
            'name: my-charm\n'
            'links:\n'
            '  documentation: https://example.com/docs\n'
            '  website:\n    - https://example.com\n'
        )
        assessment = documentation_link.assess(
            _source(tmp_path, **{'charmcraft.yaml': charmcraft})
        )
        assert assessment.verdict is Verdict.FAIL
        assert 'example.com' in assessment.rationale

    def test_documentation_matching_website_with_www_prefix_fails(self, tmp_path: pathlib.Path):
        charmcraft = (
            'name: my-charm\n'
            'links:\n'
            '  documentation: https://www.example.com/docs\n'
            '  website:\n    - https://example.com\n'
        )
        assessment = documentation_link.assess(
            _source(tmp_path, **{'charmcraft.yaml': charmcraft})
        )
        assert assessment.verdict is Verdict.FAIL

    def test_documentation_on_a_different_domain_needs_a_human(self, tmp_path: pathlib.Path):
        """The real forgejo-k8s shape: documentation is the repo's own README, not the app's."""
        charmcraft = (
            'name: my-charm\n'
            'links:\n'
            '  documentation: https://github.com/canonical/my-charm/blob/main/README.md\n'
            '  website:\n    - https://example.org\n'
        )
        assessment = documentation_link.assess(
            _source(tmp_path, **{'charmcraft.yaml': charmcraft})
        )
        assert assessment.verdict is Verdict.NEEDS_HUMAN

    def test_documentation_with_no_website_to_compare_needs_a_human(self, tmp_path: pathlib.Path):
        charmcraft = 'name: my-charm\nlinks:\n  documentation: https://example.com/docs\n'
        assessment = documentation_link.assess(
            _source(tmp_path, **{'charmcraft.yaml': charmcraft})
        )
        assert assessment.verdict is Verdict.NEEDS_HUMAN

    def test_registered_under_its_checklist_id(self):
        assert ITEM_CHECKS['charmcraft-yaml-key-documentation'] is documentation_link


class TestConfigDefaults:
    def test_no_config_options_is_not_applicable(self, tmp_path: pathlib.Path):
        charmcraft = 'name: my-charm\n'
        assessment = config_defaults.assess(_source(tmp_path, **{'charmcraft.yaml': charmcraft}))
        assert assessment.verdict is Verdict.NOT_APPLICABLE

    def test_empty_options_block_is_not_applicable(self, tmp_path: pathlib.Path):
        charmcraft = 'name: my-charm\nconfig:\n  options: {}\n'
        assessment = config_defaults.assess(_source(tmp_path, **{'charmcraft.yaml': charmcraft}))
        assert assessment.verdict is Verdict.NOT_APPLICABLE

    def test_options_missing_a_default_need_a_human_not_a_fail(self, tmp_path: pathlib.Path):
        """PLAN.md's design note is explicit: Juju does not enforce a required option, so a

        missing `default` is evidence for a human, never a confident FAIL.
        """
        charmcraft = (
            'name: my-charm\n'
            'config:\n'
            '  options:\n'
            '    log-level:\n      type: string\n      default: info\n'
            '    admin-password:\n      type: string\n'
        )
        assessment = config_defaults.assess(_source(tmp_path, **{'charmcraft.yaml': charmcraft}))
        assert assessment.verdict is Verdict.NEEDS_HUMAN
        assert 'admin-password' in assessment.rationale
        assert 'log-level' not in assessment.rationale

    def test_options_missing_a_default_are_named_in_evidence(self, tmp_path: pathlib.Path):
        charmcraft = (
            'name: my-charm\nconfig:\n  options:\n    admin-password:\n      type: string\n'
        )
        assessment = config_defaults.assess(_source(tmp_path, **{'charmcraft.yaml': charmcraft}))
        assert any('admin-password' in line for line in assessment.evidence)

    def test_every_option_with_a_default_still_needs_a_human_not_a_pass(
        self, tmp_path: pathlib.Path
    ):
        """No PASS branch exists - "best defaults" is broader than "every option has one"."""
        charmcraft = (
            'name: my-charm\n'
            'config:\n'
            '  options:\n'
            '    log-level:\n      type: string\n      default: info\n'
        )
        assessment = config_defaults.assess(_source(tmp_path, **{'charmcraft.yaml': charmcraft}))
        assert assessment.verdict is Verdict.NEEDS_HUMAN
        assert 'all with a `default` key' in assessment.rationale

    def test_explicit_null_default_still_counts_as_having_a_default(self, tmp_path: pathlib.Path):
        """`default: null` is a present key with a null value, not a missing key."""
        charmcraft = (
            'name: my-charm\n'
            'config:\n'
            '  options:\n'
            '    optional-thing:\n      type: string\n      default: null\n'
        )
        assessment = config_defaults.assess(_source(tmp_path, **{'charmcraft.yaml': charmcraft}))
        assert 'optional-thing' not in ' '.join(assessment.evidence)

    def test_registered_under_its_checklist_id(self):
        assert ITEM_CHECKS['charmcraft-yaml-key-config'] is config_defaults
