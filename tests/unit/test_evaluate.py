# Copyright 2025 Canonical Ltd.
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

"""Test the automated criteria evaluation."""

import subprocess  # noqa: S404
from unittest import mock

import pytest

import charmhub_listing_review.evaluate as evaluate


class TestGetDefaultBranch:
    @mock.patch('subprocess.run')
    def test_detects_main(self, mock_run):
        mock_run.return_value = mock.Mock(stdout='ref: refs/heads/main\tHEAD\nabc123\tHEAD\n')
        assert evaluate.get_default_branch('https://github.com/org/repo') == 'main'

    @mock.patch('subprocess.run')
    def test_detects_master(self, mock_run):
        mock_run.return_value = mock.Mock(stdout='ref: refs/heads/master\tHEAD\nabc123\tHEAD\n')
        assert evaluate.get_default_branch('https://github.com/org/repo') == 'master'

    @mock.patch('subprocess.run')
    def test_detects_custom_branch(self, mock_run):
        mock_run.return_value = mock.Mock(stdout='ref: refs/heads/26.04\tHEAD\nabc123\tHEAD\n')
        assert evaluate.get_default_branch('https://github.com/org/repo') == '26.04'

    @mock.patch('subprocess.run', side_effect=subprocess.CalledProcessError(1, 'git'))
    def test_falls_back_to_main_on_error(self, mock_run):
        assert evaluate.get_default_branch('https://github.com/org/repo') == 'main'

    @mock.patch('subprocess.run', side_effect=subprocess.TimeoutExpired('git', 5))
    def test_falls_back_to_main_on_timeout(self, mock_run):
        assert evaluate.get_default_branch('https://github.com/org/repo') == 'main'


class TestEvaluateCharmDir:
    """Test that evaluate() correctly handles the charm_dir parameter."""

    @mock.patch('charmhub_listing_review.evaluate._clone_repo')
    def test_evaluate_with_charm_dir(self, mock_clone, tmp_path):
        """evaluate() runs checks against the subdirectory."""
        charm_subdir = tmp_path / 'charms' / 'my-charm'
        charm_subdir.mkdir(parents=True)
        (charm_subdir / 'charmcraft.yaml').write_text(
            'name: my-charm\ntitle: My Charm\nsummary: A charm.\n'
            'description: A charm that does things.\n'
        )
        (charm_subdir / 'pyproject.toml').write_text('[project]\nrequires-python = ">=3.10"\n')
        mock_clone.return_value = tmp_path
        results = evaluate.evaluate(
            charm_name='my-charm',
            repository_url='https://github.com/org/my-charm-operator',
            linting_url='',
            contribution_url='',
            license_url='',
            security_url='',
            charm_dir='charms/my-charm',
        )
        # python_requires_version should pass because pyproject.toml is in the subdirectory.
        python_result = [
            r for r in results if 'requires-python' in r.lower() or 'requires_python' in r.lower()
        ]
        assert python_result
        assert python_result[0].startswith('* [x]')

    def test_evaluate_rejects_absolute_charm_dir(self):
        with pytest.raises(ValueError, match='relative path'):
            evaluate.evaluate(
                charm_name='my-charm',
                repository_url='https://github.com/org/repo',
                linting_url='',
                contribution_url='',
                license_url='',
                security_url='',
                charm_dir='/etc',
            )

    def test_evaluate_rejects_traversal_charm_dir(self):
        with pytest.raises(ValueError, match="'\\.\\.'"):
            evaluate.evaluate(
                charm_name='my-charm',
                repository_url='https://github.com/org/repo',
                linting_url='',
                contribution_url='',
                license_url='',
                security_url='',
                charm_dir='../../etc',
            )

    @mock.patch('charmhub_listing_review.evaluate._clone_repo')
    def test_evaluate_rejects_nonexistent_charm_dir(self, mock_clone, tmp_path):
        mock_clone.return_value = tmp_path
        with pytest.raises(ValueError, match='does not exist'):
            evaluate.evaluate(
                charm_name='my-charm',
                repository_url='https://github.com/org/repo',
                linting_url='',
                contribution_url='',
                license_url='',
                security_url='',
                charm_dir='nonexistent',
            )


class TestCloneRepo:
    @mock.patch('subprocess.run')
    def test_clone_without_branch(self, mock_run):
        evaluate._clone_repo('https://github.com/org/repo')
        cmd = mock_run.call_args[0][0]
        assert '--branch' not in cmd

    @mock.patch('subprocess.run')
    def test_clone_with_branch(self, mock_run):
        evaluate._clone_repo('https://github.com/org/repo', branch='develop')
        cmd = mock_run.call_args[0][0]
        assert '--branch' in cmd
        assert cmd[cmd.index('--branch') + 1] == 'develop'

    @mock.patch('subprocess.run')
    def test_clone_with_empty_branch(self, mock_run):
        evaluate._clone_repo('https://github.com/org/repo', branch='')
        cmd = mock_run.call_args[0][0]
        assert '--branch' not in cmd


@pytest.mark.parametrize(
    'name,expected',
    [
        ('valid-name', True),
        ('Invalid-Name', False),
        ('invalid--name', False),
        ('invalid_name', False),
        ('validname', True),
        ('-invalid', False),
        ('invalid-', False),
    ],
)
@pytest.mark.parametrize('method', ['action_names', 'option_names'])
def test_check_action_and_config_names(name, expected, method, tmp_path):
    charmcraft_yaml = tmp_path / 'charmcraft.yaml'
    if method == 'action_names':
        yaml_content = f"""
name: test-charm
actions:
    {name}: {{}}
"""
    else:  # option_names
        yaml_content = f"""
name: test-charm
config:
    options:
        {name}: {{}}
"""
    charmcraft_yaml.write_text(yaml_content)
    result = getattr(evaluate, method)(tmp_path)
    assert (result.startswith('* [x]')) == expected


@pytest.mark.parametrize(
    'charm_name,expected',
    [
        ('valid-name', True),
        ('Invalid-Name', False),
    ],
)
def test_check_charm_name(charm_name, expected):
    result = evaluate.check_charm_name(charm_name)
    assert (result.startswith('* [x]')) == expected


@mock.patch('requests.head')
@pytest.mark.parametrize('status,expected', [(True, True), (False, False)])
def test_contribution_guidelines(mock_head, status, expected):
    mock_head.return_value.ok = status
    result = evaluate.contribution_guidelines('url')
    assert (result.startswith('* [x]')) == expected


@mock.patch('requests.get')
@pytest.mark.parametrize('license_hash', evaluate._known_licenses)
def test_license_statement_known_license(mock_get, license_hash):
    class Response:
        ok = True
        text = 'Some License Version x.0, January 1979'

    mock_get.return_value = Response()
    with mock.patch('hashlib.sha512') as mock_hash:
        mock_hash.return_value.hexdigest.return_value = license_hash
        result = evaluate.license_statement('url')
        assert result.startswith('* [x]')


@mock.patch('requests.get')
def test_license_statement_fails(mock_get):
    mock_get.return_value.ok = False
    result = evaluate.license_statement('url')
    assert result.startswith('* [ ]')

    class Response:
        ok = True
        text = 'Some Unknown License'

    mock_get.return_value = Response()
    mock_get.return_value.ok = True
    result = evaluate.license_statement('url')
    assert result.startswith('* [ ]')


@mock.patch('requests.head')
@pytest.mark.parametrize('status,expected', [(True, True), (False, False)])
def test_security_doc(mock_head, status, expected):
    mock_head.return_value.ok = status
    result = evaluate.security_doc('url')
    assert result.startswith('* [x]') == expected


@pytest.mark.parametrize(
    'url,charm_name,expected',
    [
        ('https://github.com/canonical/foo-operator', 'foo', True),
        ('https://github.com/canonical/bar', 'foo', False),
    ],
)
def test_repository_name(url, charm_name, expected):
    result = evaluate.repository_name(url, charm_name)
    assert (result.startswith('* [x]')) == expected


def test_python_requires_version(tmp_path):
    pyproject = tmp_path / 'pyproject.toml'
    pyproject.write_text("""
    [project]
    requires-python = ">=3.10"
    """)
    result = evaluate.python_requires_version(tmp_path)
    assert result.startswith('* [x]')


def test_missing_python_requires_version(tmp_path):
    pyproject = tmp_path / 'pyproject.toml'
    pyproject.write_text("""
    [project]
    name = "foo"
    """)
    result = evaluate.python_requires_version(tmp_path)
    assert result.startswith('* [ ]')


@pytest.mark.parametrize('lock_file', ['uv.lock', 'poetry.lock'])
def test_repo_has_lock_file(tmp_path, lock_file):
    (tmp_path / 'pyproject.toml').write_text("[project]\nname = 'foo'\n")
    (tmp_path / lock_file).write_text('lock')
    result = evaluate.repo_has_lock_file(tmp_path)
    assert result.startswith('* [x]')

    tmp2 = tmp_path / 'no_repo'
    tmp2.mkdir()
    (tmp2 / 'pyproject.toml').write_text("[project]\nname = 'foo'\n")
    result = evaluate.repo_has_lock_file(tmp2)
    assert result.startswith('* [ ]')


def test_charm_has_icon(tmp_path):
    icon = tmp_path / 'icon.svg'
    icon.write_text('<svg width="100" height="100"></svg>')
    result = evaluate.charm_has_icon(tmp_path)
    assert result.startswith('* [x]')

    icon.write_text('<svg viewBox="0 0 100 100"></svg>')
    result = evaluate.charm_has_icon(tmp_path)
    assert result.startswith('* [x]')

    icon.write_text('<svg width="99" height="99"></svg>')
    result = evaluate.charm_has_icon(tmp_path)
    assert result.startswith('* [ ]')


@pytest.mark.parametrize(
    'yaml_content,link_ok,expected_checked',
    [
        # Success: all fields present, links ok.
        (
            """
name: foo
title: Foo Charm
summary: A short summary.
description: Custom description.
links:
    documentation: https://docs.example.com
    issues: https://issues.example.com
    source: https://source.example.com
    website: https://website.example.com
    contact: contact@example.com
""",
            True,
            True,
        ),
        # Failure: missing required field 'title'.
        (
            """
name: foo
summary: A short summary.
description: Custom description.
links:
    documentation: https://docs.example.com
    issues: https://issues.example.com
    source: https://source.example.com
    website: https://website.example.com
    contact: contact@example.com
""",
            True,
            False,
        ),
        # Failure: default value for 'title'.
        (
            """
name: foo
title: Charm Template
summary: A short summary.
description: Custom description.
links:
    documentation: https://docs.example.com
    issues: https://issues.example.com
    source: https://source.example.com
    website: https://website.example.com
    contact: contact@example.com
""",
            True,
            False,
        ),
        # Failure: broken link (mock returns not ok).
        (
            """
name: foo
title: Foo Charm
summary: A short summary.
description: Custom description.
links:
    documentation: https://docs.example.com
    issues: https://issues.example.com
    source: https://source.example.com
    website: https://website.example.com
    contact: contact@example.com
""",
            False,
            False,
        ),
    ],
)
@mock.patch('requests.head')
def test_metadata_links_parametrized(mock_head, tmp_path, yaml_content, link_ok, expected_checked):
    charmcraft_yaml = tmp_path / 'charmcraft.yaml'
    charmcraft_yaml.write_text(yaml_content)
    mock_head.return_value.ok = link_ok
    result = evaluate.metadata_links(tmp_path)
    assert (result.startswith('* [x]')) == expected_checked


def test_check_action_names_monorepo(tmp_path):
    """Checks work when charm files are in a subdirectory (monorepo)."""
    charm_dir = tmp_path / 'charms' / 'my-charm'
    charm_dir.mkdir(parents=True)
    charmcraft_yaml = charm_dir / 'charmcraft.yaml'
    charmcraft_yaml.write_text("""
name: my-charm
actions:
    valid-action: {}
""")
    result = evaluate.action_names(charm_dir)
    assert result.startswith('* [x]')


def test_python_requires_version_monorepo(tmp_path):
    """pyproject.toml is found in a charm subdirectory."""
    charm_dir = tmp_path / 'charms' / 'my-charm'
    charm_dir.mkdir(parents=True)
    pyproject = charm_dir / 'pyproject.toml'
    pyproject.write_text("""
[project]
requires-python = ">=3.10"
""")
    result = evaluate.python_requires_version(charm_dir)
    assert result.startswith('* [x]')


def test_charm_has_icon_monorepo(tmp_path):
    """icon.svg is found in a charm subdirectory."""
    charm_dir = tmp_path / 'charms' / 'my-charm'
    charm_dir.mkdir(parents=True)
    icon = charm_dir / 'icon.svg'
    icon.write_text('<svg width="100" height="100"></svg>')
    result = evaluate.charm_has_icon(charm_dir)
    assert result.startswith('* [x]')


@pytest.mark.parametrize(
    'yaml_content,expected_checked',
    [
        # Success: all relations have 'optional'.
        (
            """
name: foo
requires:
    database:
        interface: mysql
        optional: true
provides:
    cache:
        interface: redis
        optional: false
""",
            True,
        ),
        # Failure: missing 'optional' in requires.
        (
            """
name: foo
requires:
    database:
        interface: mysql
provides:
    cache:
        interface: redis
        optional: false
""",
            False,
        ),
        # Failure: missing 'optional' in provides.
        (
            """
name: foo
requires:
    database:
        interface: mysql
        optional: true
provides:
    cache:
        interface: redis
""",
            False,
        ),
        # Success: no relations defined.
        (
            """
name: foo
""",
            True,
        ),
    ],
)
def test_relations_includes_optional(tmp_path, yaml_content, expected_checked):
    charmcraft_yaml = tmp_path / 'charmcraft.yaml'
    charmcraft_yaml.write_text(yaml_content)
    result = evaluate.relations_includes_optional(tmp_path)
    assert (result.startswith('* [x]')) == expected_checked
