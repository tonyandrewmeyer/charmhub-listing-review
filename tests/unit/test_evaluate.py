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

from unittest import mock

import pytest

import charmhub_listing_review.evaluate as evaluate


@pytest.mark.parametrize(
    "name,expected",
    [
        ("valid-name", True),
        ("Invalid-Name", False),
        ("invalid--name", False),
        ("invalid_name", False),
        ("validname", True),
        ("-invalid", False),
        ("invalid-", False),
    ],
)
@pytest.mark.parametrize("method", ["action_names", "option_names"])
def test_check_action_and_config_names(name, expected, method, tmp_path):
    charmcraft_yaml = tmp_path / "charmcraft.yaml"
    if method == "action_names":
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
    assert (result.startswith("* [x]")) == expected


@pytest.mark.parametrize(
    "charm_name,expected",
    [
        ("valid-name", True),
        ("Invalid-Name", False),
    ],
)
def test_check_charm_name(charm_name, expected):
    result = evaluate.check_charm_name(charm_name)
    assert (result.startswith("* [x]")) == expected


@mock.patch("requests.head")
@pytest.mark.parametrize("status,expected", [(True, True), (False, False)])
def test_contribution_guidelines(mock_head, status, expected):
    mock_head.return_value.ok = status
    result = evaluate.contribution_guidelines("url")
    assert (result.startswith("* [x]")) == expected


@mock.patch("requests.get")
@pytest.mark.parametrize("license_hash", evaluate._known_licenses)
def test_license_statement_known_license(mock_get, license_hash):
    class Response:
        ok = True
        text = "Some License Version x.0, January 1979"

    mock_get.return_value = Response()
    with mock.patch("hashlib.sha512") as mock_hash:
        mock_hash.return_value.hexdigest.return_value = license_hash
        result = evaluate.license_statement("url")
        assert result.startswith("* [x]")


@mock.patch("requests.get")
def test_license_statement_fails(mock_get):
    mock_get.return_value.ok = False
    result = evaluate.license_statement("url")
    assert result.startswith("* [ ]")

    class Response:
        ok = True
        text = "Some Unknown License"

    mock_get.return_value = Response()
    mock_get.return_value.ok = True
    result = evaluate.license_statement("url")
    assert result.startswith("* [ ]")


@mock.patch("requests.head")
@pytest.mark.parametrize("status,expected", [(True, True), (False, False)])
def test_security_doc(mock_head, status, expected):
    mock_head.return_value.ok = status
    result = evaluate.security_doc("url")
    assert result.startswith("* [x]") == expected


@pytest.mark.parametrize(
    "url,charm_name,expected",
    [
        ("https://github.com/canonical/foo-operator", "foo", True),
        ("https://github.com/canonical/bar", "foo", False),
    ],
)
def test_repository_name(url, charm_name, expected):
    result = evaluate.repository_name(url, charm_name)
    assert (result.startswith("* [x]")) == expected


def test_python_requires_version(tmp_path):
    pyproject = tmp_path / "pyproject.toml"
    pyproject.write_text("""
    [project]
    requires-python = ">=3.10"
    """)
    result = evaluate.python_requires_version(tmp_path)
    assert result.startswith("* [x]")


def test_missing_python_requires_version(tmp_path):
    pyproject = tmp_path / "pyproject.toml"
    pyproject.write_text("""
    [project]
    name = "foo"
    """)
    result = evaluate.python_requires_version(tmp_path)
    assert result.startswith("* [ ]")


@pytest.mark.parametrize("lock_file", ["uv.lock", "poetry.lock"])
def test_repo_has_lock_file(tmp_path, lock_file):
    (tmp_path / "pyproject.toml").write_text("[project]\nname = 'foo'\n")
    (tmp_path / lock_file).write_text("lock")
    result = evaluate.repo_has_lock_file(tmp_path)
    assert result.startswith("* [x]")

    tmp2 = tmp_path / "no_repo"
    tmp2.mkdir()
    (tmp2 / "pyproject.toml").write_text("[project]\nname = 'foo'\n")
    result = evaluate.repo_has_lock_file(tmp2)
    assert result.startswith("* [ ]")


def test_charm_has_icon(tmp_path):
    icon = tmp_path / "icon.svg"
    icon.write_text('<svg width="100" height="100"></svg>')
    result = evaluate.charm_has_icon(tmp_path)
    assert result.startswith("* [x]")

    icon.write_text('<svg viewBox="0 0 100 100"></svg>')
    result = evaluate.charm_has_icon(tmp_path)
    assert result.startswith("* [x]")

    icon.write_text('<svg width="99" height="99"></svg>')
    result = evaluate.charm_has_icon(tmp_path)
    assert result.startswith("* [ ]")


@pytest.mark.parametrize(
    "yaml_content,link_ok,expected_checked",
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
@mock.patch("requests.head")
def test_metadata_links_parametrized(
    mock_head, tmp_path, yaml_content, link_ok, expected_checked
):
    charmcraft_yaml = tmp_path / "charmcraft.yaml"
    charmcraft_yaml.write_text(yaml_content)
    mock_head.return_value.ok = link_ok
    result = evaluate.metadata_links(tmp_path)
    assert (result.startswith("* [x]")) == expected_checked


@pytest.mark.parametrize(
    "yaml_content,expected_checked",
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
    charmcraft_yaml = tmp_path / "charmcraft.yaml"
    charmcraft_yaml.write_text(yaml_content)
    result = evaluate.relations_includes_optional(tmp_path)
    assert (result.startswith("* [x]")) == expected_checked
