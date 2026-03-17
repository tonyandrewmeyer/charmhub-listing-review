# AGENTS.md

This file provides guidance to AI agents when working with code in this repository.

## Project Overview

This repository manages the public listing review process for charms on [Charmhub](https://charmhub.io). It contains:
- GitHub issue templates for listing requests
- Automation tools for evaluating charms against listing requirements
- Infrastructure for assigning reviewers from charming teams

## Common Commands

```bash
# Run all checks (lint + unit tests)
tox

# Run linting and type checks only
tox -e lint

# Run unit tests with coverage
tox -e unit

# Run a single test
tox -e unit -- tests/unit/test_evaluate.py::test_check_charm_name

# Auto-format code
tox -e format

# Install pre-commit hooks
pre-commit install
```

## Code Architecture

### Entry Points (defined in pyproject.toml)
- `update-issue`: Updates GitHub issues with review checklists (`src/charmhub_listing_review/update_issue.py`)
- `self-review`: CLI tool for charm authors to self-check before submitting (`src/charmhub_listing_review/self_review.py`)

### Core Modules

**`evaluate.py`** - Automated charm evaluation against listing criteria. Functions clone the charm repo, check `charmcraft.yaml`, validate naming conventions, verify URLs, and return Markdown checklist items (ticked/unticked based on pass/fail).

**`update_issue.py`** - GitHub issue management. Extracts data from listing request issues, generates reviewer checklists (including best practices fetched from canonical/operator), assigns reviewers from `reviewers.yaml`, and posts/updates comments via `gh` CLI.

**`self_review.py`** - Console-friendly version of the evaluation for charm authors to run locally before submitting.

### Reviewer Assignment
`reviewers.yaml` maps GitHub usernames to charming teams. The `assign_review()` function randomly selects a team, then a reviewer from that team.

## Coding Standards

- Python 3.12+, uses uv for dependency management
- Ruff for linting/formatting with single quotes
- Type checking via ty
- Google-style docstrings
- Conventional commit messages (feat, fix, docs, ci, chore, etc.)
- New files need Apache 2.0 copyright header with current year
- Always UK English, *not* US English
- Imports belong at the top of a module. Import modules not smaller objects, other than for type annotations.

## Pull Request Guidelines

- PR titles use conventional commit format without scopes
- Rebase onto `main` before requesting review; use merge commits for subsequent updates
- Squash merge to `main` using PR title as commit message
