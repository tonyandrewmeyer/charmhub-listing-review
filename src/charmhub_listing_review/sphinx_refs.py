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

"""Convert Sphinx references to Markdown links.

This module provides functionality to convert Sphinx references
to proper Markdown links that work in GitHub issues and other Markdown contexts.
"""

_CHARMCRAFT = 'https://documentation.ubuntu.com/charmcraft/en/latest/'
_CHARMCRAFT_YAML = f'{_CHARMCRAFT}reference/files/charmcraft-yaml-file/'
_CHARMCRAFT_MANAGE = f'{_CHARMCRAFT}howto/manage-charms/'
_CHARMCRAFT_RESOURCES = f'{_CHARMCRAFT}howto/manage-resources/'
_CHARMCRAFT_MIGRATE = f'{_CHARMCRAFT}howto/migrate-plugins/'

_JUJU = 'https://documentation.ubuntu.com/juju/3.6/'
_JUJU_CLI = f'{_JUJU}reference/juju-cli/list-of-juju-cli-commands/'

_OPS = 'https://documentation.ubuntu.com/ops/latest/'
_OPS_CHARM_CODE = f'{_OPS}howto/write-and-structure-charm-code/'
_OPS_INITIALISE_PROJECT = f'{_OPS}howto/initialise-your-project/'
_OPS_MANAGE_CHARMS = f'{_OPS}howto/manage-charms/'
_OPS_MANAGE_CONFIG = f'{_OPS}howto/manage-configuration/'
_OPS_MANAGE_LIBS = f'{_OPS}howto/manage-libraries/'

# Direct mapping from Sphinx reference strings to Markdown link strings.
# Format: 'sphinx-ref-string' -> 'markdown-link-string'
# fmt: off
_SPHINX_TO_MARKDOWN: dict[str, str] = {
    # charmcraft: manage-charms
    '{external+charmcraft:ref}`initialise-a-charm`':
        f'[initialise-a-charm]({_CHARMCRAFT_MANAGE}#initialise-a-charm)',
    '{external+charmcraft:ref}`Initialise a charm <initialise-a-charm>`':
        f'[Initialise a charm]({_CHARMCRAFT_MANAGE}#initialise-a-charm)',
    '{external+charmcraft:ref}`specify-a-name`':
        f'[specify-a-name]({_CHARMCRAFT_MANAGE}#specify-a-name)',
    '{external+charmcraft:ref}`Charmcraft | Specify a name <specify-a-name>`':
        f'[Charmcraft | Specify a name]({_CHARMCRAFT_MANAGE}#specify-a-name)',
    # charmcraft: manage-resources
    '{external+charmcraft:ref}`publish-a-resource`':
        f'[publish-a-resource]({_CHARMCRAFT_RESOURCES}#publish-a-resource)',
    '{external+charmcraft:ref}`Publish a resource on Charmhub <publish-a-resource>`':
        f'[Publish a resource on Charmhub]({_CHARMCRAFT_RESOURCES}#publish-a-resource)',
    # charmcraft: charmcraft.yaml keys
    '{external+charmcraft:ref}`name <charmcraft-yaml-key-name>`':
        f'[name]({_CHARMCRAFT_YAML}#charmcraft-yaml-key-name)',
    '{external+charmcraft:ref}`actions <charmcraft-yaml-key-actions>`':
        f'[actions]({_CHARMCRAFT_YAML}#charmcraft-yaml-key-actions)',
    '{external+charmcraft:ref}`config <charmcraft-yaml-key-config>`':
        f'[config]({_CHARMCRAFT_YAML}#charmcraft-yaml-key-config)',
    '{external+charmcraft:ref}`links <charmcraft-yaml-key-documentation>`':
        f'[links]({_CHARMCRAFT_YAML}#charmcraft-yaml-key-documentation)',
    '{external+charmcraft:ref}`<endpoint role> <charmcraft-yaml-key-requires>`':
        f'[<endpoint role>]({_CHARMCRAFT_YAML}#charmcraft-yaml-key-requires)',
    # charmcraft: migration guides
    '{external+charmcraft:ref}`howto-migrate-to-uv`':
        f'[howto-migrate-to-uv]({_CHARMCRAFT_MIGRATE}charm-to-uv/#howto-migrate-to-uv)',
    '{external+charmcraft:ref}`migrate to the uv plugin <howto-migrate-to-uv>`':
        f'[migrate to the uv plugin]({_CHARMCRAFT_MIGRATE}charm-to-uv/#howto-migrate-to-uv)',
    '{external+charmcraft:ref}`howto-migrate-to-poetry`':
        f'[howto-migrate-to-poetry]({_CHARMCRAFT_MIGRATE}charm-to-poetry/#howto-migrate-to-poetry)',
    '{external+charmcraft:ref}`poetry plugin <howto-migrate-to-poetry>`':
        f'[poetry plugin]({_CHARMCRAFT_MIGRATE}charm-to-poetry/#howto-migrate-to-poetry)',
    # juju
    '{external+juju:ref}`juju model-config <command-juju-model-config>`':
        f'[juju model-config]({_JUJU_CLI}model-config/#command-juju-model-config)',
    # ops: local references (from best-practices.txt in operator repo)
    '[Add Python dependencies to pyproject.toml and update the lock file](#define-the-required-dependencies)':  # noqa: E501
        f'[Add Python dependencies to pyproject.toml and update the lock file]'
        f'({_OPS_CHARM_CODE}#define-the-required-dependencies)',
    '[Use the Python provided by the base](#define-the-required-dependencies)':
        f'[Use the Python provided by the base]'
        f'({_OPS_CHARM_CODE}#define-the-required-dependencies)',
    '[Create a repository](#create-a-repository)':
        f'[Create a repository]({_OPS_INITIALISE_PROJECT}#create-a-repository)',
    '[](#decide-your-charms-name)':
        f'[Decide your charm\'s name]({_OPS_INITIALISE_PROJECT}#decide-your-charms-name)',
    '[Validate your charm with every change](#validate-your-charm-with-every-change)':
        f'[Validate your charm with every change]'
        f'({_OPS_CHARM_CODE}#validate-your-charm-with-every-change)',
    '[Develop your charm](#develop-your-charm)':
        f'[Develop your charm]({_OPS_MANAGE_CHARMS}#develop-your-charm)',
    '[Define a configuration option](#define-a-configuration-option)':
        f'[Define a configuration option]({_OPS_MANAGE_CONFIG}#define-a-configuration-option)',
    '[Write a library](#manage-libraries-write-a-library)':
        f'[Write a library]({_OPS_MANAGE_LIBS}#manage-libraries-write-a-library)',
}
# fmt: on


def convert_sphinx_refs(text: str) -> str:
    """Convert Sphinx references to Markdown links.

    Uses a direct string replacement approach with a hardcoded mapping.

    Args:
        text: The text containing Sphinx references.

    Returns:
        The text with Sphinx references converted to Markdown links.
    """
    for sphinx_ref, markdown_link in _SPHINX_TO_MARKDOWN.items():
        text = text.replace(sphinx_ref, markdown_link)
    return text
