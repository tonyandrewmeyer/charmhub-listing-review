# Requests for public listing on charmhub.io

Charms on [Charmhub](https://charmhub.io) are either privately listed, meaning
that they can be deployed and their page viewed only if you know the name of the
charm, or publicly listed, meaning that they can be found when searching (either
on Charmhub itself or a more general web search leading to Charmhub).

Anyone can publish a charm to Charmhub, and when first published it will be
privately listed. To change the charm to be publicly listed requires passing a
lightweight review process to ensure charm consistency and quality. This is a
one-off process (in most circumstances), not done for each revision of a charm.

Reviewing charms encourages the involvement of the community. "Community"
refers to individuals and organisations creating or contributing to charms,
Juju, and the wider charming ecosystem. The goals of the review are:

1. Be transparent about the capabilities and qualities of a charm.
2. Ensure a consistent level of quality for users of charms.

A listing review is *not* code review. The reviewer may be looking at some of
the charm code, and may have comments on it, but the listing review is not a
review of the architecture or design of the charm, and is not a line-by-line
review of the charm code.

This repository contains:

* Issues that are requests for changing a charm to be publicly listed.
* Infrastructure to support the review process (such as automatically assigning
  reviews, tools to check some criteria automatically, and so on).

## Steps of a review

1. The author requests a review for *one* charm at a time with all prerequisites
   using a [listing request issue](https://github.com/canonical/charmhub-listing-review/issues/new?template=listing-request.yml)
   in this repository.
2. The reviewer checks if the prerequisites are met and the issue is
   ready.
3. The public review is carried out as a conversation on the issue.
4. The review concludes if the charm is 'publication ready', and if so the store
   team is asked to list the charm.

The result of the process is that:
* if the review is successful, the charm is switched to listed mode, or
* if the review is unsuccessful, the charm does not reach the required criteria
  and the charm remains unlisted, until the issues are resolved.

## Get ready

Read the [documentation](https://documentation.ubuntu.com/ops/latest/howto/request-public-listing/)
for detailed information about publicly listed charms, the review process, and
the criteria for public listing.

You can also use the tooling from this repository to see how close the charm is
to passing a review. Note that some of the criteria can be checked automatically
(and those will be when running the tool), but others will be manually checked
by the reviewer (so you will need to evaluate readiness in those areas
yourself).

### Self-review tool

Run the self-review tool locally to check your charm against the listing
requirements before submitting:

```bash
self-review --charm-name <name> --repository <repo-url>
```

Optional flags:

| Flag | Description |
|---|---|
| `--code-review` | Enable AI-powered code quality analysis (slower) |
| `--interactive` | Launch an interactive AI assistant after the review |
| `--ai-backend copilot\|snap\|auto` | Choose the AI backend (default: `auto`) |

### AI-powered features

The self-review tool includes optional AI-powered features that provide
actionable guidance beyond the automated pass/fail checks:

- **Failure explanations** — when checks fail, the AI explains why and suggests
  specific fixes.
- **Review summary** — a prioritised overview of the charm's readiness for
  listing.
- **Documentation assessment** — evaluates whether the README and docs are
  clear, complete, and well-structured.
- **Metadata assessment** — checks whether the `summary`, `description`, and
  `title` fields in `charmcraft.yaml` are well-written.
- **Code quality analysis** — analyses charm source code for antipatterns,
  missing error handling, and Ops framework misuse (opt-in with
  `--code-review`).
- **Interactive assistant** — a conversational REPL where you can ask follow-up
  questions about the review results (opt-in with `--interactive`).

AI features require one of two backends:

- **GitHub Copilot** — install with `uv sync --group ai`. Requires the
  `copilot` CLI tool to be installed and authenticated.
- **Canonical inference snap** — install a snap such as `sudo snap install
  gemma3`. The snap runs a local model with an OpenAI-compatible API — no
  authentication or internet access required. See the
  [inference snaps documentation](https://documentation.ubuntu.com/inference-snaps/)
  for available models and hardware requirements.

When neither backend is available, the tool falls back to its standard
behaviour and all AI sections are omitted.

### Update-issue tool

The `update-issue` tool is used in CI to update GitHub issues with review
checklists. It supports the same `--ai-backend` flag.

## Next steps

If the charm is ready for review,
[open an issue in this repository](https://github.com/canonical/charmhub-listing-review/issues/new).

Or:

- Read our [Code of conduct](https://ubuntu.com/community/code-of-conduct) and
  join our [Matrix chat](https://matrix.to/#/#charmhub-charmdev:ubuntu.com)  to get
  help polishing your charm or with the public listing review process. We also have a
  [Discourse forum](https://discourse.charmhub.io/) for longer-form questions, updates,
  and tips.
- Read our [CONTRIBUTING guide](https://github.com/canonical/charmhub-listing-review/blob/main/CONTRIBUTING.md) and contribute!
