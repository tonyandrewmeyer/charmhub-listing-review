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

## Phase 6: Canonical Inference Snap Backend

**Goal:** Add support for Canonical inference snaps as an alternative AI
backend, so the tool can run with a local model instead of requiring GitHub
Copilot.

**Depends on:** Phase 0 (ai_client.py).

### 6.1: Investigation Findings

#### Available Snaps

| Snap | Model | Input/Output | Hardware |
|---|---|---|---|
| `deepseek-r1` | DeepSeek R1 | Text → Text (reasoning) | Intel GPU/NPU/CPU, NVIDIA, Ampere |
| `gemma3` | Gemma 3 | Text+Image → Text | CPU, Intel, NVIDIA |
| `nemotron-3-nano` | Nemotron 3 Nano | Text → Text | CPU, NVIDIA |
| `qwen-vl` | Qwen 2.5 VL | Text+Image → Text | Intel, NVIDIA, Ampere |

All snaps expose an **OpenAI-compatible API** at
`http://localhost:<port>/<base-path>` with no authentication. Standard
`/chat/completions` and `/models` endpoints. Install is
`sudo snap install <name>`. Run `<snap-name> status` to discover the API URL.

See: https://documentation.ubuntu.com/inference-snaps/

#### Phase Compatibility Assessment

| Phase | Copilot (GPT-4.1) | Snap (local models) | Notes |
|---|---|---|---|
| 1 — Failure explanations | Excellent | Good | Short, structured output — local models handle this well. |
| 2 — Review summary | Excellent | Good | Summarisation is a strength of most models. |
| 3a — Doc quality | Excellent | Adequate | Needs nuanced judgement; smaller models may miss subtleties. |
| 3b — Metadata quality | Excellent | Good | Relatively constrained evaluation task. |
| 4 — Code review | Excellent | Marginal | Needs deep understanding of Juju/Ops patterns; smaller models struggle. |
| 5 — Interactive assistant | Excellent | Adequate | Multi-turn coherence varies with model size. |

**Recommendation:** All phases should be available with both backends. Users
can choose based on their quality/privacy/availability trade-offs. No features
should be artificially restricted to one backend.

### 6.2: Backend Abstraction (`ai_backend.py`, new module)

Introduce a protocol that both backends implement:

```python
class AIBackend(typing.Protocol):
    async def start(self) -> None: ...
    async def stop(self) -> None: ...
    async def send_message(
        self,
        system_message: str,
        prompt: str,
    ) -> str: ...
    def is_available(self) -> bool: ...
```

- `send_message` combines session creation and prompt sending into a single
  call. This is the only interaction pattern the high-level functions need.
  The interactive module (Phase 5) needs multi-turn conversation, so add a
  session-based interface as well:

```python
class AISession(typing.Protocol):
    async def send(self, prompt: str) -> str: ...

class AIBackend(typing.Protocol):
    # ... (as above)
    async def create_session(self, system_message: str) -> AISession: ...
```

### 6.3: `CopilotBackend` implementation

Refactor the existing `ai_client.py` low-level functions (`start_client`,
`stop_client`, `create_session`, `send_prompt`) into a class implementing
`AIBackend`. The high-level domain functions (`explain_failures`,
`generate_summary`, etc.) remain as module-level functions but accept a
backend instance instead of calling the low-level functions directly.

### 6.4: `SnapBackend` implementation (`snap_backend.py`, new module)

- Uses the `openai` Python library with a custom `base_url` pointing to the
  local snap API endpoint.
- `is_available()` checks whether the configured endpoint responds to
  `GET /models`.
- `send_message()` calls `/chat/completions` with the system message and
  user prompt.
- `create_session()` returns a `SnapSession` that accumulates messages for
  multi-turn conversation.
- No authentication required — uses a placeholder API key
  (`api_key='not-needed'`).

Configuration:

- `SNAP_API_URL` environment variable, or auto-discovered by checking
  common snap ports.
- `SNAP_MODEL` environment variable, or auto-discovered via `GET /models`.

### 6.5: Backend Selection

- New CLI option: `--ai-backend copilot|snap|auto` (default: `auto`).
- `auto` tries Copilot first (if SDK installed), then snap (if endpoint
  reachable), then disables AI.
- Environment variable `CHARMHUB_REVIEW_AI_BACKEND` as an alternative to
  the CLI flag.
- `is_ai_available()` updated to reflect the selected backend's
  availability.
- `print_ai_unavailable_notice()` updated to suggest both installation
  methods.

### 6.6: Dependency Changes

- Add `openai` to a new optional dependency group in `pyproject.toml`:
  `[dependency-groups] snap-ai = ["openai"]`
- The `openai` library is only imported when the snap backend is selected.

### 6.7: Security Considerations (Snap-Specific)

- **Local-only by default.** Snaps listen on `127.0.0.1`. No data leaves
  the machine unless the user explicitly reconfigures the snap.
- **No authentication.** The local API has no auth. This is acceptable for
  a localhost service but means any local process can query it. Document
  this for users who run reviews in shared environments.
- **Model quality.** Smaller models are more susceptible to prompt injection
  and less reliable at following structured output instructions. The
  existing output sanitisation and "AI output is advisory only" design
  mitigate this, but it should be documented that snap-based results may
  be less reliable.
- **Resource usage.** Local inference can consume significant CPU/GPU/RAM.
  The tool should document minimum hardware recommendations for each snap.

---

## Data Flow

```
CLI args (--ai-backend copilot|snap|auto)
  │
  ▼
resolve_backend() → AIBackend           (Phase 6)
  │
  ▼
evaluate() → list[CheckResult]          (deterministic, always runs)
  │
  ├──► explain_failures(backend)        (Phase 1)
  ├──► generate_summary(backend)        (Phase 2)
  ├──► assess_documentation(backend)    (Phase 3a)
  ├──► assess_metadata(backend)         (Phase 3b)
  ├──► analyse_code(backend)            (Phase 4, opt-in)
  │
  ▼
self_review.py / update_issue.py        (renders all results)
  │
  └──► interactive REPL(backend)        (Phase 5, opt-in)
```

## Graceful Degradation

All AI features are optional. When no backend is available:

- `is_ai_available()` returns `False`
- All AI calls are skipped
- Output is identical to non-AI behaviour
- A single info line is printed suggesting how to install the Copilot SDK
  or a Canonical inference snap

## Backward Compatibility

There is no need for backward compatibility in the codebase itself. When
refactoring (e.g., the `CheckResult` migration), all internal consumers and
tests should be updated directly — no `__str__` shims, no deprecated aliases.

The only compatibility constraint is the **human-facing GitHub issue format**:
the markdown checklist posted to review issues must remain the same style
(`* [x]` / `* [ ]` items) so that the existing reviewer workflow in GitHub
is not disrupted.

## Security Considerations

The review tool processes arbitrary charm repositories submitted by external
authors. Sending untrusted content to an LLM introduces several attack
surfaces that must be considered.

### Prompt Injection

**Threat:** A malicious charm could embed instructions in its README,
charmcraft.yaml description, code comments, or file names that attempt to
hijack the LLM's behaviour — for example, instructing it to always report
"all checks pass" or to output misleading guidance.

**Mitigations:**

- **Structured prompts with clear boundaries.** System prompts should
  explicitly instruct the model to treat all repository content as untrusted
  data, not as instructions. Use delimiters (e.g. triple backticks, XML tags)
  to separate user-controlled content from system instructions.
- **Output validation.** Do not use raw LLM output to make pass/fail
  decisions. All deterministic checks remain code-based; AI output is
  advisory only and clearly labelled as such.
- **Output sanitisation.** Strip or escape any HTML/markdown from AI
  responses before inserting them into GitHub issue comments to prevent
  injection into the issue rendering.
- **Review AI output.** Human reviewers should treat AI-generated sections
  (summaries, explanations, assessments) as suggestions, not authoritative
  judgements.

### Data Exfiltration

**Threat:** A crafted charm could include prompt injection payloads that
attempt to make the LLM leak sensitive data from the environment — for
example, environment variables, API tokens, file contents outside the repo,
or details about the review infrastructure.

**Mitigations:**

- **Restrict tool capabilities.** Any `@define_tool` functions (e.g.
  `read_file`) must be strictly scoped to the cloned repository directory.
  Path traversal (e.g. `../../etc/passwd`) must be blocked by validating
  that resolved paths stay within the repo root.
- **No environment access.** Tools must not expose environment variables,
  system information, or network access to the LLM.
- **Ephemeral clones.** The repository is cloned into a temporary directory
  and deleted after evaluation. No persistent state from one review leaks
  into another.
- **Copilot SDK isolation.** The Copilot CLI handles authentication
  separately; the SDK session should not have access to credentials beyond
  what is needed for the API call itself.
- **Snap backend isolation.** The snap backend communicates only with
  `localhost`. No data leaves the machine unless the user has explicitly
  reconfigured the snap to listen on a network interface.

### Denial of Service / Resource Exhaustion

**Threat:** A charm repository could contain extremely large files, deeply
nested directories, or content designed to cause excessive LLM token usage.

**Mitigations:**

- **Content size limits.** All content sent to the LLM is truncated:
  README to ~4000 chars, code files to ~3000 chars each, maximum 10 code
  files. These limits are enforced before prompts are constructed.
- **Timeout handling.** All async LLM calls should have reasonable timeouts
  to prevent indefinite hangs.
- **Graceful failure.** All AI calls are wrapped in try/except and treated
  as best-effort. A slow or failing LLM call never blocks the deterministic
  review.

### Code Execution

**Threat:** The review tool reads and analyses charm code but should never
execute it. A malicious charm could attempt to trick the tool or the LLM
into running arbitrary code.

**Mitigations:**

- **No eval/exec.** The tool never evaluates charm code — it only reads
  file contents as strings.
- **LLM sandboxing.** The Copilot SDK does not have the ability to execute
  arbitrary code on the host. Custom tools are the only way the LLM can
  interact with the system, and these are explicitly defined and scoped.
- **Static analysis only.** Phase 4 (code quality) analyses code by sending
  source text to the LLM, not by importing or running it.

### Supply Chain

**Threat:** The `github-copilot-sdk` or `openai` dependencies could be
compromised, or typosquatted packages could be installed instead.

**Mitigations:**

- **Dependency pinning.** Use locked dependency files (`uv.lock`) to pin
  exact versions.
- **Optional dependency groups.** The SDK is in an optional `ai` group and
  the OpenAI library is in an optional `snap-ai` group, so environments
  that do not need AI features are not exposed to these dependencies.

### Cross-Review Contamination

**Threat:** If the LLM client or session is reused across reviews, context
from one charm review could leak into another.

**Mitigations:**

- **Fresh sessions per review.** Each review run creates new backend
  sessions (Copilot or snap). The client is started and stopped within
  each AI function call.
- **No persistent state.** No review data is cached between runs.

## Testing Strategy

- Mock the Copilot SDK entirely in unit tests.
- Test `is_ai_available()` returns `False` when the SDK is not installed.
- Test prompt construction and result formatting.
- Test that each AI function returns sensible defaults when unavailable.
- Test `collect_charm_code()` and `doc_quality_context()` with tmp_path fixtures.
- Update all existing tests directly for the `CheckResult` return type (no
  backward-compat wrappers needed).
- Test `AIBackend` protocol compliance for both `CopilotBackend` and
  `SnapBackend`.
- Test `SnapBackend.is_available()` with mocked HTTP responses.
- Test backend resolution logic (`auto`, explicit selection, fallback).
- Test `SnapSession` multi-turn message accumulation.
