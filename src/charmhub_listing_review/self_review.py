#! /usr/bin/env python3

# /// script
# dependencies = [
#   "pyyaml",
#   "requests"
# ]
# ///

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

"""Self-review tool for charm listing requirements.

This tool allows charm authors to perform a self-check of their charm against
the listing requirements before submitting a listing request. It performs the
same automated checks as the update-issue tool but outputs the results to the
console in a user-friendly format instead of updating a GitHub issue.
"""

import argparse
import asyncio
import sys
import textwrap

from .ai_backend import print_ai_unavailable_notice, resolve_backend
from .ai_client import (
    assess_documentation,
    assess_metadata,
    explain_and_summarise,
)
from .ai_code_review import analyse_code
from .evaluate import CheckResult, EvaluationResult, evaluate
from .interactive import run_interactive
from .sphinx_refs import convert_sphinx_refs
from .update_issue import issue_comment


def format_checklist_for_console(
    checklist_markdown: str,
    ai_explanations: dict[str, str] | None = None,
) -> str:
    """Format the markdown checklist for console output.

    Args:
        checklist_markdown: The markdown checklist string.
        ai_explanations: Optional mapping of unchecked description -> AI explanation.
    """
    if ai_explanations is None:
        ai_explanations = {}
    lines = checklist_markdown.split('\n')
    formatted_lines = []
    for line in lines:
        if line.strip().startswith('* [x]'):
            item_text = line.replace('* [x]', '').strip()
            formatted_lines.append(f' ✅ {item_text}')
        elif line.strip().startswith('* [o]'):
            item_text = line.replace('* [o]', '').strip()
            formatted_lines.append(f' ❌ {item_text}')
            # Show AI explanation if available for this failed check.
            unchecked_key = line.strip().replace('* [o]', '* [ ]')
            explanation = ai_explanations.get(unchecked_key)
            if explanation:
                wrapped = textwrap.fill(
                    explanation, width=80, initial_indent='    ', subsequent_indent='    '
                )
                formatted_lines.append(f'\033[2m{wrapped}\033[0m')  # dim text
        elif line.strip().startswith('* [ ]'):
            item_text = line.replace('* [ ]', '').strip()
            formatted_lines.append(f' ❓ {item_text}')
        elif line.strip().startswith('###'):
            header_text = line.strip().replace('###', '').strip()
            formatted_lines.append(f'\n📋 \033[1m\033[4m{header_text}\033[0m')  # bold, underlined
            formatted_lines.append('')
        elif line.strip() and not line.startswith('```'):
            formatted_lines.append(f' {line.strip()}')
    return '\n'.join(formatted_lines)


def print_self_review_results(
    charm_name: str,
    project_repo: str = '',
    ci_linting: str = '',
    code_review: bool = False,
    ai_backend_choice: str = 'auto',
) -> EvaluationResult | None:
    """Print the self-review results to console.

    Returns the EvaluationResult if evaluation was performed, for use by
    interactive mode.
    """
    backend = resolve_backend(ai_backend_choice)

    print(f"\n\033[1m🔍 Charmhub Public Listing Self-Review for '{charm_name}'\033[0m")
    print('=' * (45 + len(charm_name)))

    comment = issue_comment(
        charm_name,
        '',  # demo_url is not used.
        '',  # ci_release_url is not used.
        '',  # ci_integration_url is not used.
        '',  # documentation_link is not used.
    )
    # The initial items need to have the links removed.
    fixed_checks = """
    ### Basic Requirements
* [ ] The charm does what it is meant to do, demonstrated in a demo or by following a tutorial.
* [ ] The charm's page on Charmhub provides a quality impression. The overall appearance looks good and the documentation looks reasonable.
* [ ] The charm has an icon.
* [ ] Automated releasing to unstable channels exists
* [ ] Integration tests exist, are run on every change to the default branch, and are passing. At minimum, the tests verify that the charm can be deployed and ends up in a success state, and that the charm can be integrated with at least one example for each 'provides' and 'requires' specified (including optional, excluding tracing) ending up in a success state. The tests should be run with `charmcraft test`.

    ### Documentation
    """.strip()  # noqa: E501
    comment = (
        fixed_checks + '\n\n' + comment.split('### Documentation', 1)[1].split('</details>', 1)[0]
    )

    # TODO: it would be great if we had a better wrapping story, both for GitHub and console.
    comment = comment.replace('are also\nrequired for listing.', 'are also required for listing.')

    results: list[CheckResult] = []
    charmcraft_data: dict | None = None
    doc_context: dict = {}
    code_context: dict = {}
    ai_explanations: dict[str, str] = {}

    if project_repo:
        # Like update-issue, this assumes it's GitHub for now.
        contribution_url = f'{project_repo}/blob/main/CONTRIBUTING.md'
        license_url = f'{project_repo}/blob/main/LICENSE'
        security_url = f'{project_repo}/blob/main/SECURITY.md'

        try:
            evaluation = evaluate(
                charm_name,
                project_repo,
                ci_linting or '',
                contribution_url,
                license_url,
                security_url,
                collect_code=backend is not None and code_review,
            )
            results = evaluation.checks
            charmcraft_data = evaluation.charmcraft_data
            doc_context = evaluation.doc_context
            code_context = evaluation.code_context

            for result in results:
                if not result.description:
                    continue

                description = convert_sphinx_refs(result.description)
                unchecked_version = description.replace('* [x]', '* [ ]')
                if unchecked_version in comment:
                    if result.passed:
                        comment = comment.replace(unchecked_version, description)
                    elif result.passed is False:
                        failed_version = unchecked_version.replace('* [ ]', '* [o]')
                        comment = comment.replace(unchecked_version, failed_version)
                    # passed is None means indeterminate, leave as '* [ ]' (unknown)

        except Exception as e:
            print('\n⚠️  Warning: Could not run automated checks on repository.')
            print(
                '   This may happen if the repository is not accessible, not a charm repository,'
                '   or missing required charm files like charmcraft.yaml.'
            )
            if 'No such file or directory' in str(e) and 'charmcraft.yaml' in str(e):
                print('   The repository appears to be missing a charmcraft.yaml file.')
            elif 'returned non-zero exit status' in str(e):
                print('   Could not clone the repository.')
            else:
                print(f'   Error details: {e}')

        # Run AI explanations and summary in a single event loop (best-effort).
        ai_summary = ''
        if results and backend is not None:
            try:
                results, ai_summary = asyncio.run(
                    explain_and_summarise(backend, charm_name, results)
                )
                for result in results:
                    if result.ai_explanation:
                        description = convert_sphinx_refs(result.description)
                        unchecked_key = description.replace('* [x]', '* [ ]')
                        ai_explanations[unchecked_key] = result.ai_explanation
            except Exception:  # noqa: S110
                pass  # AI features are best-effort; don't disrupt the output.

    formatted_checklist = format_checklist_for_console(comment, ai_explanations)
    print(formatted_checklist)

    if ai_explanations:
        print(
            '\n\033[33m⚠️  AI output is a suggestion only. '
            'AI makes mistakes — please check the AI responses carefully '
            'before acting on them.\033[0m'
        )

    completed_count = comment.count('* [x]')
    failed_count = comment.count('* [o]')
    unknown_count = comment.count('* [ ]')

    print(
        f'\n\033[1m📊 Progress: {completed_count} passed, {failed_count} failed, '
        f'{unknown_count} manual review needed\033[0m'
    )

    # Print AI-driven outputs when available.
    if results and backend is not None and ai_summary:
        print('\n\033[1m🤖 AI Review Summary\033[0m')
        print('-' * 40)
        print(ai_summary)

    if backend is not None and doc_context:
        try:
            doc_assessment = asyncio.run(assess_documentation(backend, doc_context))
            if doc_assessment:
                print('\n\033[1m📄 AI Documentation Assessment\033[0m')
                print('-' * 40)
                print(doc_assessment)
        except Exception:  # noqa: S110
            pass

    if backend is not None and charmcraft_data:
        try:
            meta_assessment = asyncio.run(assess_metadata(backend, charmcraft_data))
            if meta_assessment:
                print('\n\033[1m📝 AI Metadata Assessment\033[0m')
                print('-' * 40)
                print(meta_assessment)
        except Exception:  # noqa: S110
            pass

    if backend is not None and code_review and code_context.get('code_files'):
        try:
            code_analysis = asyncio.run(analyse_code(backend, code_context['code_files']))
            if code_analysis:
                print('\n\033[1m🔬 AI Code Quality Analysis\033[0m')
                print('-' * 40)
                print(code_analysis)
        except Exception:  # noqa: S110
            pass

    if backend is None:
        print_ai_unavailable_notice()

    print('\n💡 Note: This self-review covers automated checks only.')
    print('   A human reviewer will perform additional checks during the official review process.')
    print('\n📋 To submit your charm for official review, create an issue at:')
    print(
        '   https://github.com/canonical/charmhub-listing-review/issues/new?'
        'template=listing-request.yml'
    )

    if results:
        return EvaluationResult(
            checks=results,
            charmcraft_data=charmcraft_data,
            doc_context=doc_context,
            code_context=code_context,
        )
    return None


def main():
    """Main entry point for the self-review tool."""
    parser = argparse.ArgumentParser(
        description='Perform a self-review of charm listing requirements.',
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )

    parser.add_argument('--charm-name', required=True, help='Name of the charm to review')
    parser.add_argument(
        '--repository',
        required=True,
        help='URL of the charm repository (e.g., https://github.com/<user>/<workload>-operator)',
    )
    parser.add_argument('--ci-linting-url', help='URL to CI linting workflow')
    parser.add_argument(
        '--code-review',
        action='store_true',
        help='Enable AI code quality analysis (slower)',
    )
    parser.add_argument(
        '--interactive',
        action='store_true',
        help='Launch interactive AI assistant after review',
    )
    parser.add_argument(
        '--ai-backend',
        choices=['copilot', 'snap', 'auto'],
        default='auto',
        help='AI backend to use (default: auto)',
    )

    args = parser.parse_args()

    if not args.charm_name or not args.repository:
        parser.print_help()
        sys.exit(1)

    try:
        evaluation = print_self_review_results(
            charm_name=args.charm_name,
            project_repo=args.repository,
            ci_linting=args.ci_linting_url or '',
            code_review=args.code_review,
            ai_backend_choice=args.ai_backend,
        )

        if args.interactive and evaluation:
            backend = resolve_backend(args.ai_backend)
            run_interactive(args.charm_name, evaluation, backend)
        elif args.interactive:
            print(
                '\n⚠️  Interactive mode requires a repository to be specified '
                'and evaluation to succeed.'
            )
    except KeyboardInterrupt:
        print('\n\n⚡ Review cancelled by user.')
        sys.exit(1)
    except Exception as e:
        print(f'\n❌ Error during self-review: {e}')
        sys.exit(1)


if __name__ == '__main__':
    main()
