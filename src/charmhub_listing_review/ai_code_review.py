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

"""AI-powered code quality analysis for charm repositories."""

from __future__ import annotations

import pathlib
from typing import Any

from .ai_client import create_session, send_prompt, start_client, stop_client

_MAX_FILE_LENGTH = 3000

CODE_REVIEW_SYSTEM_PROMPT = """\
You are an expert in Juju charm development using the Python Ops framework. \
Analyze the provided charm source code for:

1. Common antipatterns: blocking in event handlers, hardcoded values that \
should be config, not using defer() correctly, improper status management.
2. Missing error handling: uncaught exceptions in relation-changed handlers, \
missing guards for optional relations.
3. Ops framework misuse: incorrect status setting, missing self.unit.status \
updates, not calling super().__init__() properly.
4. Security concerns: secrets in plain text, subprocess calls without input \
validation, unsafe file operations.

Return the top 5 most impactful findings as a bulleted list. For each finding, \
include:
- Severity: info, warning, or error
- A brief description of the issue
- A specific fix suggestion

If the code looks good, say so briefly. Be constructive and practical.\
"""


def collect_charm_code(repo_dir: pathlib.Path) -> dict[str, str]:
    """Collect Python source files from a charm repository.

    Looks for charm code in standard locations: src/, lib/charms/, and
    the root directory. Files are truncated to keep prompt sizes manageable.

    Args:
        repo_dir: Path to the cloned charm repository.

    Returns:
        Dictionary mapping relative file paths to their (possibly truncated) content.
    """
    code_files: dict[str, str] = {}

    # Standard charm source locations.
    search_paths = [
        repo_dir / 'src',
        repo_dir / 'lib',
    ]

    # Also check for charm.py in the root.
    root_charm = repo_dir / 'charm.py'
    if root_charm.is_file():
        _add_file(code_files, repo_dir, root_charm)

    for search_path in search_paths:
        if not search_path.is_dir():
            continue
        for py_file in sorted(search_path.rglob('*.py')):
            _add_file(code_files, repo_dir, py_file)
            # Cap total files to avoid huge prompts.
            if len(code_files) >= 10:
                break
        if len(code_files) >= 10:
            break

    return code_files


def _add_file(
    code_files: dict[str, str],
    repo_dir: pathlib.Path,
    file_path: pathlib.Path,
) -> None:
    """Read a file and add it to the code_files dict, truncating if needed."""
    try:
        content = file_path.read_text(encoding='utf-8')
        relative = str(file_path.relative_to(repo_dir))
        code_files[relative] = content[:_MAX_FILE_LENGTH]
    except (OSError, UnicodeDecodeError):
        pass


async def analyze_code(code_context: dict[str, str]) -> str:
    """Analyze charm code for quality issues using AI.

    Args:
        code_context: Dictionary mapping file paths to their content,
            as returned by collect_charm_code().

    Returns:
        A markdown-formatted analysis string, or empty string if no code found.
    """
    if not code_context:
        return ''

    files_text = '\n\n'.join(
        f'### {path}\n```python\n{content}\n```' for path, content in code_context.items()
    )

    prompt = f'Analyze the following charm source code:\n\n{files_text}'

    await start_client()
    try:
        session = await create_session(CODE_REVIEW_SYSTEM_PROMPT)
        return await send_prompt(session, prompt)
    finally:
        await stop_client()


def collect_code_context(repo_dir: pathlib.Path) -> dict[str, Any]:
    """Gather code context from a repo for AI analysis.

    This is called during evaluate() while the repo is still available.

    Returns:
        Dictionary with 'code_files' key mapping to the collected code.
    """
    code_files = collect_charm_code(repo_dir)
    return {'code_files': code_files}
