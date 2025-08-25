We welcome contributions to the Charmhub listing review process!

Before working on changes, please consider [opening an issue](https://github.com/canonical/charmhub-listing-review/issues) explaining your use case. If you would like to chat with us about your use cases or proposed implementation, you can reach us at [Matrix](https://matrix.to/#/#charmhub-charmdev:ubuntu.com) or [Discourse](https://discourse.charmhub.io/).

# Recommendations for process changes

## Best practices

If you've identified a charming best practice that is not currently found, or that needs adjusting, in the [Ops](https://ops.readthedocs.io) ([repo](https://github.com/canonical/operator)), [Juju](https://juju.is/docs) ([repo](https://github.com/juju/juju)), or [Charmcraft](https://canonical-charmcraft.readthedocs-hosted.com/latest/) ([repo](https://github.com/canonical/charmcraft)) documentation, please suggest it by opening a documentation pull request in those repositories.

Best practice notes should be added to the doc where charmers are most likely to discover them. For Charmcraft, this tends to be the [`charmcraft.yaml` reference](https://canonical-charmcraft.readthedocs-hosted.com/latest/reference/files/charmcraft-yaml-file/), and for Ops, this tends to be the [relevant how-to guide](https://ops.readthedocs.io/en/latest/howto/index.html).

In a Markdown (.md) document, the note should be added using a triple-backtick admonition block. For example:

````markdown

```{admonition} Best practice
:class: hint

Capture output to `stdout` and `stderr` in your charm and use the logging and
warning functionality to send messages to the charm user, rather than rely on
Juju capturing output.
```

````

In a reStructuredText (.rst) document, the note should be added with an admonition block. For example:

```rst
.. admonition:: Best practice
    :class: hint

    Prefer lowercase alphanumeric names, and use hyphens (-) to separate words. For
    charms that have already standardised on underscores, it is not necessary to
    change them, and it is better to be consistent within a charm then to have
    some action names be dashed and some be underscored.
```

The text in the block should be concise and suitable to be collated in a list of best practices (without the surrounding context). Place the block at an appropriate point in the document, avoiding multiple blocks (including notes, tips, cautions, and other admonitions) in a row.

## Other listing requirements

To propose adding, changing, or removing a requirement for public listing on Charmhub, open a pull request in this repository.

If more information will be required to evaluate the requirement, add a new field to [the issue template](./.github/ISSUE_TEMPLATE/listing-request.yaml). Where possible, the evaluation should use the information on the Charmhub page or in the source repository, to minimise the number of fields required when requesting a review.

If the requirement can be automatically evaluated, add an evaluation method to [the evaluation script](./evaluate.py). We do not use AI/LLM tools for evaluation at this time.

If the requirement cannot be automatically evaluated, add it to the [issue script](./update_issue.py), in the part of the script that defines the issue description template.

# Pull requests

Changes are proposed as [pull requests on GitHub](https://github.com/canonical/charmhub-listing-review/pulls).

Pull requests should have a short title that follows the [conventional commit style](https://www.conventionalcommits.org/en/) using one of these types:

- chore
- ci
- docs
- feat
- fix
- perf
- refactor
- revert
- test

Some examples:

- feat: automate subprocess best practice check
- fix!: correct the validation of unit tests passing
- docs: clarify the instructions for proposing a new best practice
- ci: adjust the workflow that annotates listing requests

We consider this project too small to use scopes, so we don't use them.

Note that the commit messages to the PR's branch do not need to follow the conventional commit format, as these will be squashed into a single commit to `main` using the PR title as the commit message.

To help us review your changes, please rebase your pull request onto the `main` branch before you request a review. If you need to bring in the latest changes from `main` after the review has started, please use a merge commit.

# Coding style

We have a team [Python style guide](https://github.com/canonical/operator/blob/main/STYLE.md), most of which is enforced by CI checks.

# Copyright

The format for copyright notices is documented in the [LICENSE](LICENSE). New files should begin with a copyright line with the current year (e.g. Copyright 2025 Canonical Ltd.) and include the full boilerplate (see APPENDIX of [LICENSE](LICENSE)). The copyright information in existing files does not need to be updated when those files are modified - only the initial creation year is required.
