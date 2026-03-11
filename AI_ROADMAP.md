# AI Features Roadmap

This document outlines the implementation plan for adding AI-powered review
capabilities to the charmhub-listing-review tool using the
[GitHub Copilot SDK](https://github.com/github/copilot-sdk).

## Phase 0: Shared Infrastructure

Foundation work that unblocks all subsequent phases.

### 0.1: `ai_client.py` (new module)

Owns all Copilot SDK interaction and provides graceful degradation.

- `is_ai_available() -> bool` — checks if the SDK is importable and the
  Copilot CLI is reachable. Result is cached.
- `get_session(system_message: str) -> CopilotSession` — creates a Copilot
  client and session with the given system prompt (model: `gpt-4.1`). Caches
  the client instance.
- `async send_prompt(session, prompt: str) -> str` — wraps `send_and_wait`,
  returns the text response.
- When AI is unavailable, a single info line is printed:
  `"Note: AI-powered features are disabled (Copilot SDK not available)."`
  and the tool falls back to existing behavior.

### 0.2: `ai_tools.py` (new module)

`@define_tool` decorated functions that the Copilot SDK can invoke, allowing
the LLM to call back into the codebase for data.

- `read_file(repo_path, file_path)` — reads a file from the cloned charm repo
  (bounded to prevent reading arbitrary system files).
- `list_files(repo_path, glob)` — lists files matching a glob in the repo.
- `get_charmcraft_yaml(repo_path)` — returns parsed charmcraft.yaml.
- `get_check_results(results)` — formats the deterministic check results for
  the LLM.

### 0.3: Refactor `evaluate()` return type

Change `evaluate()` from returning `list[str]` to `list[CheckResult]`:

```python
@dataclasses.dataclass
class CheckResult:
    name: str              # e.g. "license_statement"
    passed: bool | None    # True=pass, False=fail, None=indeterminate
    description: str       # the markdown checklist line
    context: dict          # extra data for AI (e.g. {"url": "...", "status_code": 404})
    ai_explanation: str = ""  # filled in by AI layer
```

No backward compatibility is needed in the code — all consumers
(`self_review.py`, `update_issue.py`, and tests) should be updated directly
to work with `CheckResult` objects. The GitHub issue format seen by human
reviewers must remain unchanged (same markdown checklist style).

### 0.4: Dependency changes

- Add `github-copilot-sdk` as an optional dependency group in `pyproject.toml`:
  `[dependency-groups] ai = ["github-copilot-sdk"]`
- Add new CLI entry points as needed in later phases.

---

## Phase 1: Smart Failure Explanations

**Goal:** When checks fail, generate contextual, actionable fix instructions
instead of generic messages.

**Scope:** Simplest AI integration — validates the full SDK pipeline
end-to-end.

**Implementation:**

- Add `async explain_failures(results: list[CheckResult]) -> list[CheckResult]`
  to `ai_client.py`.
- For each failed result, prompt the LLM with the check name, description, and
  context data.
- Returns 2-3 sentence actionable fix instructions.
- Populate `CheckResult.ai_explanation` with the response.

**Integration:**

- `self_review.py` — display explanations as indented text below each failed
  check.
- `update_issue.py` — append AI explanations as sub-bullets under failed items
  in the issue comment.

**Example output:**

```
 X The charm provides a license statement.
   AI: The LICENSE file was not recognised as a standard open-source license.
   Ensure you are using an unmodified Apache 2.0, GPL 2/3, LGPL 3, or MPL 2.0
   license text, or open a feature request to recognise other licenses.
```

---

## Phase 2: AI-Powered Review Summary

**Goal:** After all checks run, provide a human-readable summary with
prioritised action items.

**Implementation:**

- Add `async generate_summary(charm_name, results, charmcraft_data) -> str`
  to `ai_client.py`.
- System prompt instructs the LLM to write 3-5 bullet points summarising
  readiness for public listing, prioritised by impact.
- Input includes: all CheckResult data, charmcraft.yaml metadata, and
  pass/fail/unknown counts.

**Integration:**

- `self_review.py` — new "AI Review Summary" section after the progress stats.
- `update_issue.py` — prepend summary to issue comment in a collapsible
  `<details><summary>AI Summary</summary>...</details>` block.

**Example output:**

```
AI Review Summary
-----------------
- PRIORITY: Fix the 3 failed checks (license, contribution guide, security
  doc) before submitting. These are typically quick fixes.
- The charm metadata looks complete but the description could be more specific
  about what distinguishes this charm.
- 5 checks require manual review. Focus on integration test coverage first.
```

---

## Phase 3: Documentation and Metadata Quality Assessment

Two independent features that can be developed in parallel. Both follow the
same pattern: read content from the cloned repo, send to LLM for qualitative
assessment, append results.

### Phase 3a: Documentation Quality

**Goal:** Evaluate whether README/docs are actually good (clear, complete,
has examples, etc.), beyond just checking that URLs resolve.

**Implementation:**

- Add `doc_quality_context(repo_dir) -> dict` to `evaluate.py` — collects
  README.md content (truncated to ~4000 chars), docs/ directory listing,
  documentation URL from charmcraft.yaml.
- Add `async assess_documentation(doc_context) -> str` to `ai_client.py`.
- LLM evaluates: clarity, completeness (installation, configuration, usage,
  troubleshooting), presence of code examples, proper formatting.
- Returns a brief assessment (pass/needs-work/fail) with 2-3 specific
  suggestions.
- The LLM session includes the `read_file` tool so it can request additional
  doc files if needed.

### Phase 3b: Metadata Quality

**Goal:** Evaluate whether `summary`, `description`, `title`, and other text
fields in charmcraft.yaml are well-written and descriptive.

**Implementation:**

- Add `async assess_metadata(charmcraft_data) -> str` to `ai_client.py`.
- LLM evaluates text fields for clarity, informativeness, and best practices.
- Provides specific rewrite suggestions where needed.

**Example output:**

```
AI Metadata Assessment
----------------------
- Summary: Good. Clear and concise.
- Description: Needs work. Too generic — consider mentioning specific
  integrations (for example, "integrates with PostgreSQL and S3").
- Title: "My Charm" should be more descriptive.
```

**Integration (both 3a and 3b):**

- New sections in `self_review.py` output after the main checklist.
- New sections in `update_issue.py` issue comments.

---

## Phase 4: Code Quality Analysis

**Goal:** Analyse the charm's Python code for antipatterns, missing error
handling, and Juju/Ops framework misuse.

**Implementation:**

- New module: `ai_code_review.py`
  - `collect_charm_code(repo_dir) -> dict` — finds and reads the main charm
    class, files under `src/`, charm libraries under `lib/`. Truncates each
    file to ~3000 chars. Returns `{filepath: content}`.
  - `async analyse_code(code_context) -> str` — sends code to Copilot with a
    specialised system prompt covering:
    1. Common antipatterns (blocking in event handlers, hardcoded values)
    2. Missing error handling (uncaught exceptions in relation handlers)
    3. Ops framework misuse (incorrect status setting, missing `defer()`)
    4. Security concerns (secrets in plain text, unsafe subprocess calls)
  - Returns top 5 findings with severity (info/warning/error) and fix
    suggestions.
  - LLM session includes `read_file` and `list_files` tools for repo
    exploration.

**Integration:**

- `self_review.py` — opt-in via `--code-review` flag (off by default, slower).
  Display results in a "Code Quality" section.
- `update_issue.py` — collapsible `<details>` section in issue comments.

---

## Phase 5: Interactive Self-Review Assistant

**Goal:** Turn `self-review` into a conversational tool where authors can ask
follow-up questions about failures and get guidance.

**Depends on:** All prior phases (uses them as callable tools).

**Implementation:**

- New module: `interactive.py`
  - Async REPL loop maintaining a Copilot session.
  - Session initialised with: charm name, repository URL, all check results,
    and any AI assessments already generated.
  - Custom tools registered via `@define_tool`:
    - `run_check(check_name)` — re-runs a specific check.
    - `read_charm_file(path)` — reads a file from the cloned repo.
    - `explain_check(check_name)` — detailed info about what a check does.
    - `suggest_fix(check_name)` — generates fix suggestion (reuses Phase 1).
    - `assess_docs()` — runs doc quality assessment (reuses Phase 3a).
    - `assess_metadata()` — runs metadata assessment (reuses Phase 3b).
    - `review_code(file_path)` — reviews a specific file (reuses Phase 4).

**Integration:**

- New entry point in `pyproject.toml`:
  `interactive-review = "charmhub_listing_review.interactive:main"`
- `self_review.py` — `--interactive` flag launches the interactive mode after
  showing initial results.

**Example interaction:**

```
$ self-review --charm-name foo --repository https://github.com/org/foo-operator --interactive

[... normal self-review output ...]

Interactive Review Assistant (type 'quit' to exit)

> Why did the license check fail?
The license file was found but its content hash didn't match any recognised
license. Your LICENSE file may have been modified from the standard text.

> Can you check if my README has usage examples?
[reads README.md] Your README includes an installation section but lacks usage
examples. Consider adding a "Usage" section showing how to deploy and configure
the charm with `juju deploy`.

> quit
```

---

## Phase 6: Canonical Inference Snap Backend (Investigation)

**Goal:** Investigate whether any of the Canonical inference snaps (which
expose an OpenAI-compatible API) can be used as an alternative backend to
GitHub Copilot for some or all of the AI features.

**Implementation:**

- Research which Canonical inference snaps are available and what models they
  provide.
- Create a backend abstraction (or wrapper) so that the existing Copilot
  integration and the snap-based backend can be used interchangeably.
- Add a CLI option (for example, `--ai-backend copilot|snap`) to let the user
  choose which backend to use.
- The snap backend would use the OpenAI-compatible API exposed by the snap,
  wrapped to match the same interface used by the Copilot SDK integration.
- Evaluate whether all phases (1-5) work acceptably with the snap models or
  whether some features should be Copilot-only.

---

## Data Flow

```
CLI args
  │
  ▼
evaluate() → list[CheckResult]          (deterministic, always runs)
  │
  ├──► explain_failures()               (Phase 1)
  ├──► generate_summary()               (Phase 2)
  ├──► assess_documentation()           (Phase 3a)
  ├──► assess_metadata()                (Phase 3b)
  ├──► analyse_code()                   (Phase 4, opt-in)
  │
  ▼
self_review.py / update_issue.py        (renders all results)
  │
  └──► interactive REPL                 (Phase 5, opt-in)
```

## Graceful Degradation

All AI features are optional. When the Copilot SDK or CLI is not available:

- `ai_client.is_ai_available()` returns `False`
- All AI calls are skipped
- Output is identical to current behavior
- A single info line is printed suggesting how to install the SDK

## Backward Compatibility

There is no need for backward compatibility in the codebase itself. When
refactoring (e.g., the `CheckResult` migration), all internal consumers and
tests should be updated directly — no `__str__` shims, no deprecated aliases.

The only compatibility constraint is the **human-facing GitHub issue format**:
the markdown checklist posted to review issues must remain the same style
(`* [x]` / `* [ ]` items) so that the existing reviewer workflow in GitHub
is not disrupted.

## Testing Strategy

- Mock the Copilot SDK entirely in unit tests.
- Test `is_ai_available()` returns `False` when the SDK is not installed.
- Test prompt construction and result formatting.
- Test that each AI function returns sensible defaults when unavailable.
- Test `collect_charm_code()` and `doc_quality_context()` with tmp_path fixtures.
- Update all existing tests directly for the `CheckResult` return type (no
  backward-compat wrappers needed).
