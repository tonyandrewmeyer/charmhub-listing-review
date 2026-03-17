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

"""Evaluate a charm for listing on Charmhub.

This script automates as much of the charm listing review process as possible,
providing the results to the review in a comment on the listing request GitHub
issue.

Not all checks for listing can be automated. This script provides a head-start
for the reviewer, and is also a way for charm publishers to check their charm
against the listing requirements before submitting a listing request.
"""

import hashlib
import pathlib
import re
import shutil
import subprocess  # noqa: S404
import tempfile
import tomllib
import xml.etree.ElementTree as ET  # noqa: S405
from typing import Any

import requests
import yaml

from ._models import CheckResult, EvaluationResult
from .ai_code_review import collect_code_context


def evaluate(
    charm_name: str,
    repository_url: str,
    linting_url: str,
    contribution_url: str,
    license_url: str,
    security_url: str,
    collect_code: bool = False,
) -> EvaluationResult:
    """Evaluate the charm for listing on Charmhub.

    Returns an EvaluationResult containing CheckResult objects for each
    automated check, plus repo context data for AI analysis.
    """
    results: list[CheckResult] = []
    repo_dir = _clone_repo(repository_url)
    try:
        results.append(coding_conventions(linting_url))
        results.append(contribution_guidelines(contribution_url))
        results.append(license_statement(license_url))
        results.append(security_doc(security_url))
        results.append(metadata_links(repo_dir))
        results.append(check_charm_name(charm_name))
        results.append(action_names(repo_dir))
        results.append(option_names(repo_dir))
        results.append(repository_name(repository_url, charm_name))
        results.append(relations_includes_optional(repo_dir))
        results.append(charmcraft_tooling(repo_dir))
        results.append(charm_plugin_strict_dependencies(repo_dir))
        results.append(python_requires_version(repo_dir))
        results.append(repo_has_lock_file(repo_dir))
        results.append(charm_has_icon(repo_dir))
        results.append(charm_lib_docs(repo_dir))

        charmcraft_data = _get_charmcraft_yaml(repo_dir)
        doc_context = _gather_doc_context(repo_dir, charmcraft_data)

        code_context = collect_code_context(repo_dir) if collect_code else {}
    finally:
        shutil.rmtree(str(repo_dir), ignore_errors=True)
    return EvaluationResult(
        checks=results,
        charmcraft_data=charmcraft_data,
        doc_context=doc_context,
        code_context=code_context,
    )


def coding_conventions(linting_url: str) -> CheckResult:
    """Checks for coding conventions are reasonable and implemented in CI.

    The source code of the charm is accessible in the sense of approachability.
    Consistent source code style and formatting are also considered a sign of
    being committed to quality.
    """
    # We'll work on automating this in the future. Before we do that, we'll want
    # to figure out how much consistency there is in CI across charms, and if we
    # should encourage more.
    return CheckResult(
        name='coding_conventions',
        passed=None,
        description='* [ ] The charm implements coding conventions in CI.',
        context={'linting_url': linting_url},
    )


def contribution_guidelines(contribution_url: str) -> CheckResult:
    """The documentation for contribution resolves with a 2xx status code.

    The documentation for contributing to the charm should be separate from the
    documentation for developing or using the charm.
    """
    description = '* [ ] The charm provides contribution guidelines.'
    context: dict[str, Any] = {'url': contribution_url}
    # Ideally, this would also check that the content of the URL is actually a
    # reasonable contribution guide, but that is more difficult to automate.
    try:
        response = requests.head(contribution_url, allow_redirects=True, timeout=5)
        context['status_code'] = response.status_code
        if response.ok:
            return CheckResult(
                name='contribution_guidelines',
                passed=True,
                description=description.replace('* [ ]', '* [x]'),
                context=context,
            )
        return CheckResult(
            name='contribution_guidelines', passed=False, description=description, context=context
        )
    except requests.RequestException as e:
        context['error'] = str(e)
        return CheckResult(
            name='contribution_guidelines', passed=False, description=description, context=context
        )


_known_licenses = {
    'fdae7ed259455ca9fa45939e7f25cbb4de29831cda16d9151de25a5f6e9d9be43b053f4fd3b896026239fca77abce04f543d591c501ecf4ce18c854bc0a51660',  # Apache 2.0  # noqa: E501
    '5ae83c5b0ac7ed6469b38ed11f33b3d1dfabc9eaee8fff6a2e3d5e23b45e5f899a2bec93865c33868e83d0c8e4bff2c0dd0ebf0c3a390903a1f4d9ac7d9ab29e',  # GPL 2  # noqa: E501
    '56a2f53e2df8adf4b55edf328579a74b1358f7f177b5242bd97dd79a8d26bc93f9dcc96dbdd6854627a96b73deb9ccaada6862f581ad1c8f6a2f3fe0849db005',  # GPL 3  # noqa: E501
    '0906b47a8ae8ec763c6e548f42582d82fd8c8fa62403cd2b00a94d547277c98e65ce9d505d476b707c10c8aacd2d8094c594ba1e12d3c67cd658981c4bd2fe83',  # LGPL 3  # noqa: E501
    'f5a0456e775e047c6c91571cf004a42cd04b3962ee882bc7c23d62a9a4d95bb310bfaaeb6a16bd777990eb564cc6c9ef13d7b3028f0d62ed2636ca083de6439a',  # MPL 2.0  # noqa: E501
}


def license_statement(license_url: str) -> CheckResult:
    """The charm's license statement resolves with a 2xx status code.

    For the charm shared, OSS or not, the licensing terms of the charm are
    clarified (which also implies an identified authorship of the charm).
    """
    description = '* [ ] The charm provides a license statement.'
    context: dict[str, Any] = {'url': license_url}
    try:
        response = requests.get(license_url, allow_redirects=True, timeout=5)
        context['status_code'] = response.status_code
        if response.ok:
            # Check for known licenses, with a simple hash.
            license_hash = hashlib.sha512(response.text.strip().encode('utf-8')).hexdigest()
            context['license_hash'] = license_hash
            if license_hash in _known_licenses:
                return CheckResult(
                    name='license_statement',
                    passed=True,
                    description=description.replace('* [ ]', '* [x]'),
                    context=context,
                )
            context['known_license'] = False
            # If it's another license, then let the reviewer decide if it's a license file.
        return CheckResult(
            name='license_statement', passed=False, description=description, context=context
        )
    except requests.RequestException as e:
        context['error'] = str(e)
        return CheckResult(
            name='license_statement', passed=False, description=description, context=context
        )


def security_doc(security_url: str) -> CheckResult:
    """The charm's security documentation resolves with a 2xx status code.

    The charm's security documentation explains which versions are supported,
    and how to report security issues.
    """
    description = '* [ ] The charm provides a security statement.'
    context: dict[str, Any] = {'url': security_url}
    # Ideally, this would also check some of the content of the security doc,
    # like that it has a section on how to report security issues.
    try:
        response = requests.head(security_url, allow_redirects=True, timeout=5)
        context['status_code'] = response.status_code
        if response.ok:
            return CheckResult(
                name='security_doc',
                passed=True,
                description=description.replace('* [ ]', '* [x]'),
                context=context,
            )
        return CheckResult(
            name='security_doc', passed=False, description=description, context=context
        )
    except requests.RequestException as e:
        context['error'] = str(e)
        return CheckResult(
            name='security_doc', passed=False, description=description, context=context
        )


def _clone_repo(charm_repo_url: str) -> pathlib.Path:
    """Clone the charm repository to a temporary directory."""
    temp_dir = tempfile.mkdtemp()
    try:
        subprocess.run(
            ['/usr/bin/git', 'clone', '--depth', '1', charm_repo_url, temp_dir],
            check=True,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        return pathlib.Path(temp_dir)
    except subprocess.CalledProcessError:
        shutil.rmtree(temp_dir)
        raise


def _get_charmcraft_yaml(repo_dir: pathlib.Path) -> dict[Any, Any] | None:
    charmcraft_path = repo_dir / 'charmcraft.yaml'
    if not charmcraft_path.is_file():
        return None
    try:
        with charmcraft_path.open() as f:
            return yaml.safe_load(f)
    except (yaml.YAMLError, OSError):
        return None


_MAX_DOC_CONTENT_LENGTH = 4000


def _gather_doc_context(
    repo_dir: pathlib.Path,
    charmcraft_data: dict[Any, Any] | None,
) -> dict[str, Any]:
    """Gather documentation context from the repo for AI quality assessment."""
    context: dict[str, Any] = {}

    readme_path = repo_dir / 'README.md'
    if readme_path.is_file():
        try:
            content = readme_path.read_text(encoding='utf-8')
            context['readme_content'] = content[:_MAX_DOC_CONTENT_LENGTH]
        except OSError:
            pass

    docs_dir = repo_dir / 'docs'
    if docs_dir.is_dir():
        doc_files = [str(p.relative_to(repo_dir)) for p in docs_dir.rglob('*.md')]
        context['doc_files'] = doc_files[:50]  # Cap the list.

    if charmcraft_data:
        links = charmcraft_data.get('links', {})
        doc_url = links.get('documentation', '')
        if doc_url:
            context['documentation_url'] = doc_url

    return context


def metadata_links(repo_dir: pathlib.Path) -> CheckResult:
    """charmcraft.yaml includes the name, title, summary, and description.

    A complete and consistent appearance of the charm is required.

    The repository contains a `charmcraft.yaml` file that includes fields for
    name, title, summary, and description that are not the default profile
    values. A links field includes fields for documentation, issues, source,
    website, and contact, which all resolve with a 2xx status code.
    """
    description = '* [ ] charmcraft.yaml includes required metadata.'
    context: dict[str, Any] = {}
    data = _get_charmcraft_yaml(repo_dir)
    if not data:
        context['error'] = 'charmcraft.yaml not found or invalid'
        return CheckResult(
            name='metadata_links', passed=False, description=description, context=context
        )
    default_desc = """A single sentence that says what the charm is, concisely and memorably.

A paragraph of one to three short sentences, that describe what the charm does.

A third paragraph that explains what need the charm meets.

Finally, a paragraph that describes whom the charm is useful for.\n"""
    required_fields = {
        'name': '',
        'title': 'Charm Template',
        'summary': 'A very short one-line summary of the charm.',
        'description': default_desc,
    }
    missing_fields = []
    for field, default in required_fields.items():
        value = data.get(field, '')
        if not value or value == default:
            missing_fields.append(field)
    if missing_fields:
        context['missing_or_default_fields'] = missing_fields
        return CheckResult(
            name='metadata_links', passed=False, description=description, context=context
        )

    links = data.get('links', {})
    link_fields = ['documentation', 'issues', 'source', 'website', 'contact']
    broken_links: dict[str, str] = {}
    for field in link_fields:
        url = links.get(field)
        # Contact only needs to be a string.
        if field == 'contact':
            continue
        if not url:
            broken_links[field] = 'missing'
            continue
        try:
            resp = requests.head(url, allow_redirects=True, timeout=5)
            if not resp.ok:
                broken_links[field] = f'status {resp.status_code}'
        except requests.RequestException as e:
            broken_links[field] = str(e)
    if broken_links:
        context['broken_links'] = broken_links
        return CheckResult(
            name='metadata_links', passed=False, description=description, context=context
        )

    return CheckResult(
        name='metadata_links',
        passed=True,
        description=description.replace('* [ ]', '* [x]'),
        context=context,
    )


def _validate_action_or_config_name(name: str) -> bool:
    """Validate that the action or config name follows best practices."""
    if not name.islower():
        return False
    if not all(c.isalnum() or c == '-' for c in name):
        return False
    if '--' in name or name.startswith('-') or name.endswith('-'):
        return False
    return True


def check_charm_name(charm_name: str) -> CheckResult:
    """The charm's name is aligns with best practices.

    The charm's name is lowercase alphanumeric, with hyphens (-) to separate
    words. The charm name is not the same as the repository name.
    """
    # This has to match the description in the Charmcraft documentation.
    description = re.sub(
        r'\s+',
        ' ',
        """
    * [ ] The charm name should be slug-oriented (ASCII lowercase letters, numbers, and hyphens)
    and follow the pattern ``<workload name in full>[<function>][-k8s]``. For example,
    ``argo-server-k8s``. Include the ``-k8s`` suffix on all charms that run on a Kubernetes cloud,
    unless the charm has no workload or you know that there will never be a machine version of the
    charm. Don't include an organization or publisher in the name. Don't add an ``operator`` or
    ``charm`` prefix or suffix. For naming a repository, see
    {external+charmcraft:ref}`initialise-a-charm`.
    See {external+charmcraft:ref}`name <charmcraft-yaml-key-name>`.
    """,
    ).strip()
    passed = _validate_action_or_config_name(charm_name)
    return CheckResult(
        name='check_charm_name',
        passed=passed,
        description=description.replace('* [ ]', '* [x]') if passed else description,
        context={'charm_name': charm_name},
    )


def action_names(repo_dir: pathlib.Path) -> CheckResult:
    """The charm's actions are named according to the best practices.

    The charm's actions are named using lowercase alphanumeric names, with
    hyphens (-) to separate words.

    The repository contains a `charmcraft.yaml` file that includes an actions
    field, and each action is named appropriately.
    """
    # This has to match the description in the Charmcraft documentation.
    description = re.sub(
        r'\s+',
        ' ',
        """
    * [ ] Prefer lowercase alphanumeric action names, and use hyphens (-) to separate words.
    For charms that have already standardised on underscores, it is not necessary to change them,
    and it is better to be consistent within a charm then to have some action names be dashed and
    some be underscored. See {external+charmcraft:ref}`actions <charmcraft-yaml-key-actions>`.
    """,
    ).strip()
    data = _get_charmcraft_yaml(repo_dir)
    if not data or 'actions' not in data:
        # No actions means that everything is fine in terms of names.
        return CheckResult(
            name='action_names',
            passed=True,
            description=description.replace('* [ ]', '* [x]'),
        )
    actions = data.get('actions', {})
    invalid_names = [n for n in actions if not _validate_action_or_config_name(n)]
    if invalid_names:
        return CheckResult(
            name='action_names',
            passed=False,
            description=description,
            context={'invalid_names': invalid_names},
        )
    return CheckResult(
        name='action_names',
        passed=True,
        description=description.replace('* [ ]', '* [x]'),
    )


def option_names(repo_dir: pathlib.Path) -> CheckResult:
    """The charm's config options are named according to the best practices.

    The charm's config options are named using lowercase alphanumeric names,
    with hyphens (-) to separate words.

    The repository contains a `charmcraft.yaml` file that includes a config
    field, itself containing an options field, and each option is named
    appropriately.
    """
    # This has to match the description in the Charmcraft documentation.
    description = re.sub(
        r'\s+',
        ' ',
        """
    * [ ] Prefer lowercase alphanumeric option names, separated with dashes if required.
    For charms that have already standardised on underscores, it is not necessary to change them,
    and it is better to be consistent within a charm then to have some config names be dashed and
    some be underscored. See {external+charmcraft:ref}`config <charmcraft-yaml-key-config>`.
    """,
    ).strip()
    data = _get_charmcraft_yaml(repo_dir)
    if not data or 'config' not in data:
        # No options means that everything is fine in terms of names.
        return CheckResult(
            name='option_names',
            passed=True,
            description=description.replace('* [ ]', '* [x]'),
        )
    options = data.get('config', {}).get('options', {})
    invalid_names = [n for n in options if not _validate_action_or_config_name(n)]
    if invalid_names:
        return CheckResult(
            name='option_names',
            passed=False,
            description=description,
            context={'invalid_names': invalid_names},
        )
    return CheckResult(
        name='option_names',
        passed=True,
        description=description.replace('* [ ]', '* [x]'),
    )


def repository_name(repository_url: str, charm_name: str) -> CheckResult:
    """The repository is named according to best practices."""
    # This has to match the description in the Charmcraft documentation.
    description = re.sub(
        r'\s+',
        ' ',
        """
    * [ ] Name the repository using the pattern ``<charm name>-operator`` for a single charm,
      or ``<base charm name>-operators`` when the repository will hold multiple related charms.
      For the charm name, see {external+charmcraft:ref}`Charmcraft | Specify a name
      <specify-a-name>`. See [Create a repository and initialise it]
      (#create-a-repository-and-initialise-it).
    """,
    ).strip()
    repo_name = repository_url.rstrip('/').split('/')[-1]
    if repo_name.endswith('.git'):
        repo_name = repo_name[:-4]
    single_pattern = f'{charm_name}-operator'
    multi_pattern = f'{charm_name}-operators'
    passed = repo_name in (single_pattern, multi_pattern)
    return CheckResult(
        name='repository_name',
        passed=passed,
        description=description.replace('* [ ]', '* [x]') if passed else description,
        context={'repo_name': repo_name, 'charm_name': charm_name},
    )


def relations_includes_optional(repo_dir: pathlib.Path) -> CheckResult:
    """The charm's relations include the optional key.

    Always include the ``optional`` key, rather than relying on the default
    value to indicate that the relation is required. Although this field is not
    enforced by Juju, including it makes it clear to users (and other tools)
    whether the relation is required.

    The charm's relations are defined in the `charmcraft.yaml` file, in requires
    and provides fields, and each relation includes the `optional` key.
    """
    # This has to match the description in the Charmcraft documentation.
    description = re.sub(
        r'\s+',
        ' ',
        """
    * [ ] Include the ``optional`` key in all endpoint definitions, rather than relying on the
    default value to indicate that the relation is required. Although this field is not enforced
    by Juju, including it makes it clear to users (and other tools) whether the relation is
    required. See {external+charmcraft:ref}`<endpoint role> <charmcraft-yaml-key-requires>`.
    """,
    ).strip()
    data = _get_charmcraft_yaml(repo_dir)
    if not data:
        return CheckResult(
            name='relations_includes_optional',
            passed=False,
            description=description,
            context={'error': 'charmcraft.yaml not found or invalid'},
        )
    missing_optional: list[str] = []
    for section in ('requires', 'provides'):
        endpoints = data.get(section, {})
        for endpoint_name, config in endpoints.items():
            if not isinstance(config, dict) or 'optional' not in config:
                missing_optional.append(f'{section}.{endpoint_name}')
    if missing_optional:
        return CheckResult(
            name='relations_includes_optional',
            passed=False,
            description=description,
            context={'missing_optional': missing_optional},
        )
    return CheckResult(
        name='relations_includes_optional',
        passed=True,
        description=description.replace('* [ ]', '* [x]'),
    )


def charmcraft_tooling(repo_dir: pathlib.Path) -> CheckResult:
    """The charm includes the expected tooling for linting and testing.

    The repository contains a Makefile, Justfile, or tox.ini that provides
    commands for formatting, linting, unit testing, and integration testing
    (other commands can also be included).
    """
    # This has to match the description in the Charmcraft documentation.
    description = re.sub(
        r'\s+',
        ' ',
        """
    * [ ] All charms should provide the commands configured by the Charmcraft profile, to allow
    easy testing across the charm ecosystem. It's fine to tweak the configuration of individual
    tools, or to add additional commands, but keep the command names and meanings that the profile
    provides. See [Develop your charm](#develop-your-charm).
    """,
    ).strip()
    context: dict[str, Any] = {}
    tooling_files = ['Makefile', 'Justfile', 'tox.ini']
    for filename in tooling_files:
        if (repo_dir / filename).is_file():
            context['tooling_file'] = filename
            break
    else:
        context['error'] = 'no tooling file found (Makefile, Justfile, or tox.ini)'
        return CheckResult(
            name='charmcraft_tooling', passed=False, description=description, context=context
        )

    # Check for commands in the files
    commands = {'format', 'lint', 'unit', 'integration'}
    found_commands: set[str] = set()
    commands_to_run: list[list[str]] = []
    file_path = repo_dir / filename
    with file_path.open('r', encoding='utf-8') as f:
        content = f.read().lower()

    if filename == 'Makefile' or filename == 'Justfile':
        for command in commands:
            if f'{command}:' in content or f'{command} (' in content:
                found_commands.add(command)
                if command != 'integration':
                    commands_to_run.append(['make' if filename == 'Makefile' else 'just', command])
    elif filename == 'tox.ini':
        if shutil.which('tox'):
            tox_cmd = ['tox']
        elif shutil.which('uvx'):
            tox_cmd = ['uvx', '--with', 'tox-uv', 'tox']
        else:
            tox_cmd = ['tox']
        for command in commands:
            if f'[testenv:{command}]' in content:
                found_commands.add(command)
                if command != 'integration':
                    commands_to_run.append([*tox_cmd, '-e', command])

    context['found_commands'] = sorted(found_commands)
    context['missing_commands'] = sorted(commands - found_commands)

    for command in commands_to_run:
        try:
            subprocess.check_output(command)
        except (subprocess.CalledProcessError, FileNotFoundError):
            context['failed_command'] = command
            return CheckResult(
                name='charmcraft_tooling', passed=False, description=description, context=context
            )

    if all(command in found_commands for command in commands):
        return CheckResult(
            name='charmcraft_tooling',
            passed=True,
            description=description.replace('* [ ]', '* [x]'),
            context=context,
        )
    return CheckResult(
        name='charmcraft_tooling', passed=False, description=description, context=context
    )


def charm_plugin_strict_dependencies(repo_dir: pathlib.Path) -> CheckResult:
    """The charm plugin is configured with strict dependencies.

    When using the `charm` plugin with charmcraft, ensure that you set strict
    dependencies to true.

    The repository contains a `charmcraft.yaml` file that includes building the
    charm. If the charm uses the `charm` plugin, it should have a
    `strict-dependencies: true` field.
    """
    # TODO: This has quadruple quotes in the doc, to handle an embedded example.
    # Ideally, we can rework the docs to avoid that, rather than trying to
    # handle it here. There's another case too, that isn't automated (log
    # construction).
    # This has to match the description in the Charmcraft documentation.
    description = re.sub(
        r'\s+',
        ' ',
        """
    * [ ] When using the `charm` plugin with charmcraft, ensure that you set strict dependencies to
    true. For example:
    """,
    ).strip()
    return CheckResult(
        name='charm_plugin_strict_dependencies',
        passed=None,
        description=description,
    )


def python_requires_version(repo_dir: pathlib.Path) -> CheckResult:
    """The charm's `pyproject.toml` specifies the required Python version.

    This ensures that tooling will detect any use of Python features not
    available in the versions you support.

    The repository contains a `pyproject.toml` file that includes a
    `requires-python` field with a version specifier.
    """
    # This has to match the description in the Charmcraft documentation.
    requires_python_link = (
        '[`requires-python`](https://packaging.python.org/en/latest/'
        'specifications/pyproject-toml/#requires-python)'
    )
    description = re.sub(
        r'\s+',
        ' ',
        f"""
    * [ ] Set the {requires_python_link} version in your `pyproject.toml` so that tooling will
    detect any use of Python features not available in the versions you support.
    """,
    ).strip()
    context: dict[str, Any] = {}
    pyproject_path = repo_dir / 'pyproject.toml'
    if not pyproject_path.is_file():
        context['error'] = 'pyproject.toml not found'
        return CheckResult(
            name='python_requires_version', passed=False, description=description, context=context
        )
    try:
        with pyproject_path.open('rb') as f:
            data = tomllib.load(f)
    except Exception as e:
        context['error'] = f'failed to parse pyproject.toml: {e}'
        return CheckResult(
            name='python_requires_version', passed=False, description=description, context=context
        )
    requires_python = None
    if 'project' in data and 'requires-python' in data['project']:
        requires_python = data['project']['requires-python']
    elif 'tool' in data and 'poetry' in data['tool']:
        deps = data['tool']['poetry'].get('dependencies', {})
        requires_python = deps.get('python')
    if requires_python:
        context['requires_python'] = requires_python
        return CheckResult(
            name='python_requires_version',
            passed=True,
            description=description.replace('* [ ]', '* [x]'),
            context=context,
        )
    return CheckResult(
        name='python_requires_version', passed=False, description=description, context=context
    )


def repo_has_lock_file(repo_dir: pathlib.Path) -> CheckResult:
    """Both the pyproject.toml and lock file should be present in the repository.

    This allows reproducible builds and ensures that the charm's dependencies
    are clearly defined.
    """
    # This has to match the description in the Charmcraft documentation.
    description = re.sub(
        r'\s+',
        ' ',
        """
    * [ ] Ensure that the `pyproject.toml` *and* the lock file are committed to version control, so
    that exact versions of charms can be reproduced.
    """,
    ).strip()
    context: dict[str, Any] = {}
    lock_files = ['poetry.lock', 'uv.lock']
    if not repo_dir / 'pyproject.toml':
        context['error'] = 'pyproject.toml not found'
        return CheckResult(
            name='repo_has_lock_file', passed=False, description=description, context=context
        )
    found_lock = [lf for lf in lock_files if (repo_dir / lf).is_file()]
    if found_lock:
        context['lock_file'] = found_lock[0]
        return CheckResult(
            name='repo_has_lock_file',
            passed=True,
            description=description.replace('* [ ]', '* [x]'),
            context=context,
        )
    return CheckResult(
        name='repo_has_lock_file', passed=False, description=description, context=context
    )


def charm_has_icon(repo_dir: pathlib.Path) -> CheckResult:
    """The charm has an icon.

    Requirements:
     * Canvas size must be 100x100 pixels.
     * The icon must consist of a circle with a flat color and a logo - any other detail is up to
       you, but it's a good idea to also conform to best practices.

    Best practices:
     * Icons should have some padding between the edges of the circle and the logo.
     * Icons should not be overly complicated. Charm icons are displayed in various sizes
       (from 160x160 to 32x32 pixels) and they should be always legible.
     * Symbols should have a similar weight on all icons: Avoid too thin strokes and use the whole
       space available to draw the symbol within the limits defined by the padding. However, if the
       symbol is much wider than it is high, it may overflow onto the horizontal padding area to
       ensure its weight is consistent.
     * Do not use glossy materials unless they are parts of a logo that you are not allowed to
       modify.
    """
    description = '* [ ] The charm has an icon.'
    context: dict[str, Any] = {}
    icon_path = repo_dir / 'icon.svg'
    if not icon_path.is_file():
        context['error'] = 'icon.svg not found'
        return CheckResult(
            name='charm_has_icon', passed=False, description=description, context=context
        )
    tree = ET.parse(icon_path)  # noqa: S314
    root = tree.getroot()
    width = root.attrib.get('width')
    height = root.attrib.get('height')
    view_box = root.attrib.get('viewBox')
    if width and height:
        width_val = float(width.replace('px', ''))
        height_val = float(height.replace('px', ''))
        context['width'] = width_val
        context['height'] = height_val
        if width_val == 100 and height_val == 100:
            return CheckResult(
                name='charm_has_icon',
                passed=True,
                description=description.replace('* [ ]', '* [x]'),
                context=context,
            )
    elif view_box:
        parts = view_box.strip().split()
        if len(parts) == 4:
            vb_width = float(parts[2])
            vb_height = float(parts[3])
            context['viewBox_width'] = vb_width
            context['viewBox_height'] = vb_height
            if vb_width == 100 and vb_height == 100:
                return CheckResult(
                    name='charm_has_icon',
                    passed=True,
                    description=description.replace('* [ ]', '* [x]'),
                    context=context,
                )
    return CheckResult(
        name='charm_has_icon', passed=False, description=description, context=context
    )


def charm_lib_docs(repo_dir: pathlib.Path) -> CheckResult:
    """If the charm contains Charmhub libraries, they are appropriately documented."""
    # We don't actually automate checking this, we just provide (or not) the
    # checks the reviewer is expected to do.
    charmcraft_path = repo_dir / 'charmcraft.yaml'
    if not charmcraft_path.exists():
        return CheckResult(name='charm_lib_docs', passed=None, description='')
    try:
        with charmcraft_path.open() as f:
            data = yaml.safe_load(f)
            charm_name = data.get('name', '')
            if not charm_name:
                return CheckResult(name='charm_lib_docs', passed=None, description='')
    except (yaml.YAMLError, OSError, KeyError):
        return CheckResult(name='charm_lib_docs', passed=None, description='')
    if not list((repo_dir / 'lib' / 'charms' / charm_name).glob('*/*.py')):
        # The charm does not provide a Charmhub library, so skip including any items.
        return CheckResult(name='charm_lib_docs', passed=None, description='')
    # fmt: off
    description = (
        re.sub(
            r'\n^\n',
            ' ',
            """
If the charm provides an interface library, the library's module docstring must contain the following information:
* [ ] the interface(s) this library is for, and if it takes care of one or both of the providing/requiring sides
* [ ] guidance on how to start when using the library to implement their end of the interface

If the charm provides a general library, the library's module docstring must contain the following information:
* [ ] the purpose of the library
* [ ] the intended audience for the library: is this library intended for use only by the charm or the charming team, or is it a public library intended for anyone to use in their charm?
* [ ] guidance on how to start using the library
""".strip(),  # noqa: E501
        )
    )
    # fmt: on

    return CheckResult(
        name='charm_lib_docs',
        passed=None,
        description=description,
        context={'charm_name': charm_name},
    )
