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


def evaluate(
    charm_name: str,
    repository_url: str,
    linting_url: str,
    contribution_url: str,
    license_url: str,
    security_url: str,
) -> list[str]:
    """Evaluate the charm for listing on Charmhub.

    Returns a list of strings that should be included in a Markdown checklist.
    For example:

        * [ ] The charm foos correctly.
        * [x] The charm bars when it should.
        * [ ] Appropriate baz was found in the charm metadata.

    The items will be ticked when the automation was able to determine that the
    criteria is already met, and unticked both if it was not met and if the
    automation was unable to make a determination.
    """
    results: list[str] = []
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
    finally:
        shutil.rmtree(str(repo_dir), ignore_errors=True)
    return results


def coding_conventions(linting_url: str) -> str:
    """Checks for coding conventions are reasonable and implemented in CI.

    The source code of the charm is accessible in the sense of approachability.
    Consistent source code style and formatting are also considered a sign of
    being committed to quality.
    """
    # We'll work on automating this in the future. Before we do that, we'll want
    # to figure out how much consistency there is in CI across charms, and if we
    # should encourage more.
    return '* [ ] The charm implements coding conventions in CI.'


def contribution_guidelines(contribution_url: str) -> str:
    """The documentation for contribution resolves with a 2xx status code.

    The documentation for contributing to the charm should be separate from the
    documentation for developing or using the charm.
    """
    description = '* [ ] The charm provides contribution guidelines.'
    # Ideally, this would also check that the content of the URL is actually a
    # reasonable contribution guide, but that is more difficult to automate.
    try:
        response = requests.head(contribution_url, allow_redirects=True, timeout=5)
        if response.ok:
            return description.replace('* [ ]', '* [x]')
        return description
    except requests.RequestException:
        return description


_known_licenses = {
    'fdae7ed259455ca9fa45939e7f25cbb4de29831cda16d9151de25a5f6e9d9be43b053f4fd3b896026239fca77abce04f543d591c501ecf4ce18c854bc0a51660',  # Apache 2.0  # noqa: E501
    '5ae83c5b0ac7ed6469b38ed11f33b3d1dfabc9eaee8fff6a2e3d5e23b45e5f899a2bec93865c33868e83d0c8e4bff2c0dd0ebf0c3a390903a1f4d9ac7d9ab29e',  # GPL 2  # noqa: E501
    '56a2f53e2df8adf4b55edf328579a74b1358f7f177b5242bd97dd79a8d26bc93f9dcc96dbdd6854627a96b73deb9ccaada6862f581ad1c8f6a2f3fe0849db005',  # GPL 3  # noqa: E501
    '0906b47a8ae8ec763c6e548f42582d82fd8c8fa62403cd2b00a94d547277c98e65ce9d505d476b707c10c8aacd2d8094c594ba1e12d3c67cd658981c4bd2fe83',  # LGPL 3  # noqa: E501
    'f5a0456e775e047c6c91571cf004a42cd04b3962ee882bc7c23d62a9a4d95bb310bfaaeb6a16bd777990eb564cc6c9ef13d7b3028f0d62ed2636ca083de6439a',  # MPL 2.0  # noqa: E501
}


def license_statement(license_url: str) -> str:
    """The charm's license statement resolves with a 2xx status code.

    For the charm shared, OSS or not, the licensing terms of the charm are
    clarified (which also implies an identified authorship of the charm).
    """
    description = '* [ ] The charm provides a license statement.'
    try:
        response = requests.get(license_url, allow_redirects=True, timeout=5)
        if response.ok:
            # Check for known licenses, with a simple hash.
            license_hash = hashlib.sha512(response.text.strip().encode('utf-8')).hexdigest()
            if license_hash in _known_licenses:
                return description.replace('* [ ]', '* [x]')
            # If it's another license, then let the reviewer decide if it's a license file.
        return description
    except requests.RequestException:
        return description


def security_doc(security_url: str) -> str:
    """The charm's security documentation resolves with a 2xx status code.

    The charm's security documentation explains which versions are supported,
    and how to report security issues.
    """
    description = '* [ ] The charm provides a security statement.'
    # Ideally, this would also check some of the content of the security doc,
    # like that it has a section on how to report security issues.
    try:
        response = requests.head(security_url, allow_redirects=True, timeout=5)
        if response.ok:
            return description.replace('* [ ]', '* [x]')
        return description
    except requests.RequestException:
        return description


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


def metadata_links(repo_dir: pathlib.Path) -> str:
    """charmcraft.yaml includes the name, title, summary, and description.

    A complete and consistent appearance of the charm is required.

    The repository contains a `charmcraft.yaml` file that includes fields for
    name, title, summary, and description that are not the default profile
    values. A links field includes fields for documentation, issues, source,
    website, and contact, which all resolve with a 2xx status code.
    """
    description = '* [ ] charmcraft.yaml includes required metadata.'
    data = _get_charmcraft_yaml(repo_dir)
    if not data:
        return description
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
    for field, default in required_fields.items():
        value = data.get(field, '')
        if not value or value == default:
            return description

    links = data.get('links', {})
    link_fields = ['documentation', 'issues', 'source', 'website', 'contact']
    for field in link_fields:
        url = links.get(field)
        # Contact only needs to be a string.
        if field == 'contact':
            continue
        if not url:
            return description
        try:
            resp = requests.head(url, allow_redirects=True, timeout=5)
            if not resp.ok:
                return description
        except requests.RequestException:
            return description

    return description.replace('* [ ]', '* [x]')


def _validate_action_or_config_name(name: str) -> bool:
    """Validate that the action or config name follows best practices."""
    if not name.islower():
        return False
    if not all(c.isalnum() or c == '-' for c in name):
        return False
    if '--' in name or name.startswith('-') or name.endswith('-'):
        return False
    return True


def check_charm_name(charm_name: str) -> str:
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
    if _validate_action_or_config_name(charm_name):
        return description.replace('* [ ]', '* [x]')
    return description


def action_names(repo_dir: pathlib.Path) -> str:
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
        return description.replace('* [ ]', '* [x]')
    actions = data.get('actions', {})
    for name in actions:
        if not _validate_action_or_config_name(name):
            return description
    return description.replace('* [ ]', '* [x]')


def option_names(repo_dir: pathlib.Path) -> str:
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
        return description.replace('* [ ]', '* [x]')
    options = data.get('config', {}).get('options', {})
    for name in options:
        if not _validate_action_or_config_name(name):
            return description
    return description.replace('* [ ]', '* [x]')


def repository_name(repository_url: str, charm_name: str) -> str:
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
    if repo_name in (single_pattern, multi_pattern):
        return description.replace('* [ ]', '* [x]')
    return description


def relations_includes_optional(repo_dir: pathlib.Path) -> str:
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
        return description
    for section in ('requires', 'provides'):
        endpoints = data.get(section, {})
        for config in endpoints.values():
            if not isinstance(config, dict) or 'optional' not in config:
                return description
    return description.replace('* [ ]', '* [x]')


def charmcraft_tooling(repo_dir: pathlib.Path) -> str:
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
    tooling_files = ['Makefile', 'Justfile', 'tox.ini']
    for filename in tooling_files:
        if (repo_dir / filename).is_file():
            break
    else:
        return description

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
        for command in commands:
            if f'[testenv:{command}]' in content:
                found_commands.add(command)
                if command != 'integration':
                    commands_to_run.append(['tox', '-e', command])

    for command in commands_to_run:
        try:
            subprocess.check_output(command)
        except subprocess.CalledProcessError:
            return description

    if all(command in found_commands for command in commands):
        return description.replace('* [ ]', '* [x]')
    return description


def charm_plugin_strict_dependencies(repo_dir: pathlib.Path) -> str:
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
    return description


def python_requires_version(repo_dir: pathlib.Path) -> str:
    """The charm's `pyproject.toml` specifies the required Python version.

    This ensures that tooling will detect any use of Python features not
    available in the versions you support.

    The repository contains a `pyproject.toml` file that includes a
    `requires-python` field with a version specifier.
    """
    # This has to match the description in the Charmcraft documentation.
    requires_python = (
        '[`requires-python`](https://packaging.python.org/en/latest/'
        'specifications/pyproject-toml/#requires-python)'
    )
    description = re.sub(
        r'\s+',
        ' ',
        f"""
    * [ ] Set the {requires_python} version in your `pyproject.toml` so that tooling will detect
    any use of Python features not available in the versions you support.
    """,
    ).strip()
    pyproject_path = repo_dir / 'pyproject.toml'
    if not pyproject_path.is_file():
        return description
    try:
        with pyproject_path.open('rb') as f:
            data = tomllib.load(f)
    except Exception:
        return description
    requires_python = None
    if 'project' in data and 'requires-python' in data['project']:
        requires_python = data['project']['requires-python']
    elif 'tool' in data and 'poetry' in data['tool']:
        deps = data['tool']['poetry'].get('dependencies', {})
        requires_python = deps.get('python')
    if requires_python:
        return description.replace('* [ ]', '* [x]')
    return description


def repo_has_lock_file(repo_dir: pathlib.Path) -> str:
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
    lock_files = ['poetry.lock', 'uv.lock']
    if not repo_dir / 'pyproject.toml':
        return description
    if any((repo_dir / lock_file).is_file() for lock_file in lock_files):
        return description.replace('* [ ]', '* [x]')
    return description


def charm_has_icon(repo_dir: pathlib.Path) -> str:
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
    icon_path = repo_dir / 'icon.svg'
    if not icon_path.is_file():
        return description
    tree = ET.parse(icon_path)  # noqa: S314
    root = tree.getroot()
    width = root.attrib.get('width')
    height = root.attrib.get('height')
    view_box = root.attrib.get('viewBox')
    if width and height:
        width_val = float(width.replace('px', ''))
        height_val = float(height.replace('px', ''))
        if width_val == 100 and height_val == 100:
            return description.replace('* [ ]', '* [x]')
    elif view_box:
        parts = view_box.strip().split()
        if len(parts) == 4:
            vb_width = float(parts[2])
            vb_height = float(parts[3])
            if vb_width == 100 and vb_height == 100:
                return description.replace('* [ ]', '* [x]')
    return description


def charm_lib_docs(repo_dir: pathlib.Path) -> str:
    """If the charm contains Charmhub libraries, they are appropriately documented."""
    # We don't actually automate checking this, we just provide (or not) the
    # checks the reviewer is expected to do.
    charmcraft_path = repo_dir / 'charmcraft.yaml'
    if not charmcraft_path.exists():
        return ''
    try:
        with charmcraft_path.open() as f:
            data = yaml.safe_load(f)
            charm_name = data.get('name', '')
            if not charm_name:
                return ''
    except (yaml.YAMLError, OSError, KeyError):
        return ''
    if not (repo_dir / 'lib' / 'charms' / charm_name).glob('*/*.py'):
        # The charm does not provide a Charmhub library, so skip including any items.
        return ''
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

    return description
