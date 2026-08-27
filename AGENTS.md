# AGENTS.md

This file provides guidance to AI agents when working with code in this repository.

## Project Overview

This repository manages the public listing review process for charms on [Charmhub](https://charmhub.io). It contains:
- GitHub issue templates for listing requests
- Automation tools for evaluating charms against listing requirements
- Infrastructure for assigning reviewers from charming teams

## Common Commands

```bash
# Display help
make

# Run linting and unit tests
make all

# Perform linting, spell checking, and static type checks
make lint

# Run unit tests
make unit

# Run a single test
make unit ARGS='tests/unit/test_evaluate.py::test_check_charm_name'

# Format the Python code
make format

# Auto-fix linting and formatting issues
make fix

# Install pre-commit hooks
pre-commit install
```

Avoid using `head` and `tail` with these commands, as that masks issues.

## Code Architecture

### Entry Points (defined in pyproject.toml)
- `update-issue`: Updates GitHub issues with review checklists (`src/charmhub_listing_review/update_issue.py`)
- `self-review`: CLI tool for charm authors to self-check before submitting (`src/charmhub_listing_review/self_review.py`)

### Core Modules

**`evaluate.py`** - Automated charm evaluation against listing criteria. Functions clone the charm repo, check `charmcraft.yaml`, validate naming conventions, verify URLs, and return Markdown checklist items (ticked/unticked based on pass/fail).

**`update_issue.py`** - GitHub issue management. Extracts data from listing request issues, generates reviewer checklists (including best practices fetched from canonical/operator), assigns reviewers from `reviewers.yaml`, and posts/updates comments via `gh` CLI.

**`self_review.py`** - Console-friendly version of the evaluation for charm authors to run locally before submitting.

### Reviewer Assignment
`reviewers.yaml` lists the GitHub usernames of managers eligible to be
assigned a review. The `assign_review()` function randomly picks one of
them; the assignee is then expected to delegate the actual review to
someone in their team by mentioning them in a comment.

## Coding Standards

- Python 3.12+, uses uv for dependency management
- Ruff for linting/formatting with single quotes
- Type checking via ty
- Google-style docstrings
- Conventional commit messages (feat, fix, docs, ci, chore, etc.)
- New files need Apache 2.0 copyright header with current year

## Pull Request Guidelines

- PR titles use conventional commit format without scopes
- Rebase onto `main` before requesting review; use merge commits for subsequent updates
- Squash merge to `main` using PR title as commit message
