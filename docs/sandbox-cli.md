# Sandbox CLI

[Back to README](../README.md)

## Setup and execution

Requires Python 3.13, a running Docker engine, Git, and an OpenAI compatible or Anthropic compatible API endpoint. `ripgrep` is optional but makes search faster.

```bash
git clone https://github.com/Penelope-Liang/Mini_Agent.git
cd Mini_Agent

python3.13 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

Create a `.env` file (see [`.env.example`](../.env.example)):

```env
APIKEY=sk-your-api-key
API=https://api.deepseek.com/anthropic
MODEL=deepseek-chat
```

Build the small execution image once (it contains Python and the tool worker, not model credentials):

```bash
docker build -t miniagent-sandbox:1 sandbox
```

Start interactive task mode:

```bash
python3 -m agents.main
```

Each message starts a separate task with a fresh model context and container. The source is copied,
not mounted. No previous task edits carry over automatically. Model requests and credentials stay
in the host controller; file operations and shell commands run in the container.

Or run a single task against a chosen repository and exit:

```bash
python3 -m agents.main --workspace /path/to/repo --accept-edits "Fix the input validation bug"
```

Read-only investigation:

```bash
python3 -m agents.main --plan "How should Skills retrieval be optimized?"
```

Results are saved outside the source, under `~/.miniagent/sandbox-runs/<task>/`:
`changes.patch`, `changes.json` (base64 regular-file contents and deleted paths), `audit.jsonl`,
and `result.json` (status, transcript, tokens, cost, elapsed time). Nothing is automatically applied.
After reviewing, the patch can be applied to a separate checkout with `git apply -p2 changes.patch`.

The execution container is non-root, has no original-source mounts, no network, a read-only root filesystem,
and bounded writable tmpfs, memory, CPU, process count and task duration. Docker/image failures
never fall back to local tools. Command timeout destroys the task container. Cancellation also
cleans it up. Containers and their files are disposable; failed tasks may have no exported patch.

Snapshot defaults omit Git-ignored files, symbolic links, special files, runtime state, and common
credential filenames (`.env*`, `.npmrc`, `*.key`, etc.). This is not a secret scanner: choose the source
carefully. `.sandboxignore` adds relative glob patterns or directory prefixes ending in `/` (no negation).
This repository excludes `data/`. Limits: 2 MiB/file, 32 MiB total, 5000 files.

The minimal image has Python's standard library and shell utilities. Bake project dependencies into
a trusted image derived from `miniagent-sandbox:1`, then pass `--sandbox-image IMAGE`; runtime package
installation cannot use the network. Image configuration is trusted and must not contain credentials.
The old root Dockerfile packages the whole application; it is not this execution-only image.

For a small, explicit boundary the sandbox CLI exposes file/search/shell tools, sub-agents and context
compaction, plus opt-in Context7 HTTP queries. Other MCP connections, Skill mutation, memory writes,
slash commands and `--resume` are not
available in this mode. Existing library/evaluation paths retain their explicit local/benchmark runtimes;
when embedding `Agent`, pass a `DockerWorkspace` to obtain this isolation. Docker is not a guarantee
against kernel/container escapes or malicious output; exported changes still require review.

Main-agent approvals use cancellable terminal input. Only `y` or `yes` (case-insensitive) approves;
empty answers, EOF, and non-terminal stdin are denied. Piped input is never read as approval.
The task deadline includes approval waiting. A deadline during input produces `stopped` with
`Task timed out while waiting for approval`; other task deadlines report `Task timed out`.
Cancellation before the deadline reports `stopped` with `Task interrupted while waiting for approval`
or `Task interrupted`, without assuming whether the user or another caller cancelled the task.
Cancelled prompts detach their input listener and are recorded in the audit log as an unapproved
authorization with outcome `cancelled-while-waiting`. Local library/evaluation callbacks are unchanged.

Sub-agents ask through the same prompt, one request at a time. Each prompt title names its
originator (`Main agent`, `Sub-agent #2 (explore)`); labels are assigned by the controller, and audit
records carry `agent` and `parent_session_id`. Refusals are counted across the parent and its
sub-agents. When a sub-agent stops on repeated refusals, or the sandbox is lost, the whole task stops
with that reason; a sub-agent stopping for other reasons lets the parent try again, up to the usual
repeated-stop limit.

The prompt shows the request exactly as it will run. Model-written text is displayed literally:
Rich markup is not interpreted, and terminal control or bidirectional-override characters are shown
as visible escapes (`\x1b`, `\u202e`) without changing the executed arguments. Requests longer than
40 lines or 4000 characters show a preview with a `PREVIEW INCOMPLETE` notice naming the full request,
saved as `approvals/<call id>-<sha256 prefix>.txt` beside the audit log and recorded in the audit
entry with its hash. If the full request cannot be saved, the operation is refused without asking.
Opening the file does not approve anything. Allow a longer `--task-timeout` when reviewing large changes.

### Sandbox plan approval

Start with `--plan` to investigate without editing project files. The main agent submits the full
plan as `exit_plan_mode(plan=...)`; the controller validates non-empty text (at most 64 KiB UTF-8),
saves it to the task output directory as `plan.md`, and includes it in `result.json` as `plan`.
No host plan-file path is exposed to the model. Review the displayed content and choose:

- **Approve & Execute:** switch to `default` permissions in the same container. Tool approvals
  still apply; approving a plan does not approve its edits or commands.
- **Revise Plan:** provide feedback and remain read-only before submitting a revised plan.
- **Exit Without Executing:** stop the task with `Plan not approved; exited without executing`.

Non-terminal input or EOF exits without execution. Empty or invalid selections prompt again;
empty revision feedback also prompts again. Timeouts and cancellation use the ordinary approval
stop reasons, and the submitted plan remains in the report. Plan approval is main-agent-only.
Selected Skill tool restrictions must permit `exit_plan_mode` to use this workflow.

Design references: [Gemini CLI plan approval](https://github.com/google-gemini/gemini-cli/blob/main/packages/core/src/tools/exit-plan-mode.ts)
for decision branches and [prompt_toolkit input lifecycle](https://github.com/prompt-toolkit/python-prompt-toolkit/blob/main/src/prompt_toolkit/input/vt100.py)
for listener cleanup. MiniAgent's exit-without-execution branch uses its own hard-stop mechanism.

### Selected Skills and read-only memory

```bash
python -m agents.main --workspace /path/to/project --skill project:review "Review the login logic"
python -m agents.main --workspace /path/to/project --skill user:python-check --no-memory "Check the parser"
```

`--skill` is repeatable; a bare name means `project:NAME`. Only the selected directories are read:
`<workspace>/.miniagent/skills/NAME` or `~/.miniagent/skills/NAME`. No global discovery or automatic
skill evolution runs. Inline instructions are loaded with their scripts/templates copied into
`/workspace/.task-skills/<scope>/<name>/`; `${CLAUDE_SKILL_DIR}` points there. Relative script paths
must be resolved against that directory. These are disposable, writable copies, not host mounts;
changes to them are excluded from exported patches. The host Skill files are never updated.
Path traversal, symlinks (including parent directories), special files and oversized resources fail
closed. Combined resources are limited to 4 MiB / 200 files, 512 KiB per file. `allowed-tools`
restrictions are intersected across selected Skills and with the sandbox tool allowlist for the whole
task, including children. `context: fork` Skills currently fail explicitly instead of silently changing
semantics. No model-facing tool can load an additional host Skill during a task.

Memory recall uses only `~/.miniagent/projects/<hash>/memory`, where `<hash>` is the first 16 hex
characters of SHA-256 of the resolved `--workspace` path (the existing project-memory layout).
No fallback to the launch directory, another project, or global memories is performed. Existing valid
Markdown entries are ranked by task keyword overlap; up to five are injected within a 16 KiB body
budget. This is bounded keyword recall, not the legacy LLM-based semantic retrieval. Missing stores
are not created; invalid entries are ignored and symlinks rejected. `--no-memory` disables recall.
Memory and Skill text cannot expand tool permissions. Selected Skill and memory names are recorded
in `result.json`; the transcript contains the injected text. Automatic persistence remains disabled.

### Guarded Context7 documentation queries

```bash
python -m agents.main --workspace /path/to/project --context7 "Check pytest fixture documentation"
```

External documentation queries are **off by default**. `--context7` enables only
`mcp__context7__resolve-library-id` and `mcp__context7__query-docs`. The host connects directly to
`https://mcp.context7.com/mcp` using MCP Streamable HTTP. It does not load repository MCP configs,
run `npx`, launch local MCP servers, follow redirects, or use environment proxy settings. Docker
execution remains on `--network none`. Optional `CONTEXT7_API_KEY` is sent only as the Context7
authentication header; model credentials are not forwarded.

Each query passes strict parameter checks and the existing permission gate. Default and accept-edits
modes ask for approval; plan mode blocks external tools, and dont-ask mode denies requests needing
approval. Explicit permission rules still apply; bypass mode cannot bypass the egress checks. Skill
`allowed-tools` must include the exact Context7 tool names to allow them. Server discovery is lazy,
limited to these two tools, and uses local schemas/descriptions rather than server instructions.

Questions are limited to 500 characters, library identifiers to 128. Known credential values, common
secret patterns, email addresses, URLs, code blocks, multiline/code-like payloads, unexpected fields
and control characters are rejected before any network traffic. These conservative checks can reject
legitimate questions and are **not comprehensive data-loss prevention**: novel or encoded secrets and
short private prose may evade detection. Use generic public-library questions; omit `--context7` for
sensitive tasks. There is no automatic code upload or query generation from repository contents.

`audit.jsonl` records the endpoint, validated outgoing parameters, decisions and response outcome.
Rejected parameters are fully redacted; remote error bodies are not logged. Audit failure prevents
sending the request. Accepted parameters are logged verbatim after validation, so unrecognized
sensitive text is not guaranteed to be redacted. The ordinary model transcript in `result.json` may
still contain original model-generated arguments; egress auditing is not transcript sanitization.
Responses are bounded and returned as untrusted tool data. They cannot modify the tool allowlist or
permission policy. Calls have a 30-second deadline and an eight-attempt task budget; network/protocol
failure disables further calls for that task, with no automatic retry or local execution fallback.

Protocol references: [Context7 tools and parameters](https://context7.com/docs/resources/all-clients)
and [MCP Streamable HTTP](https://modelcontextprotocol.io/specification/2025-03-26/basic/transports).

Implementation references: [SWE-ReX DockerDeployment](https://github.com/SWE-agent/SWE-ReX/blob/main/src/swerex/deployment/docker.py)
and [AutoGen Docker executor](https://github.com/microsoft/autogen/blob/main/python/packages/autogen-ext/src/autogen_ext/code_executors/docker/_docker_code_executor.py).
The implementation here is independent and uses the Docker CLI, without importing either framework.

Legacy library features and API configuration are described in [docs/getting-started.md](getting-started.md).

## Protected acceptance tests and independent verification

For evaluation tasks, explicitly protect the files that define acceptance criteria:

```bash
python -m agents.main --workspace /path/to/project \
  --protect-path tests --protect-path pyproject.toml \
  --verify-command "python -m pytest -q tests" \
  --verify-timeout 120 "Fix the reported issue without changing acceptance tests"
```

`--protect-path` accepts existing workspace-relative files or directories and can be repeated.
It does not infer test names or accept globs. Missing, excluded, and unsafe paths fail closed.
The controller creates sanitized copies under the run output's `acceptance/` directory and mounts
only those copies read-only. The original repository is never mounted. Protection applies to shell
commands, editor tools, and sub-agents even with automatic edit approval. Protect relevant fixtures
and configuration explicitly as well; directory protection also prevents adding new files inside it.

After a normally completed run exports its candidate, `--verify-command` runs in a fresh offline
container with the original source snapshot plus exported changes and protected acceptance files.
Model messages cannot choose the command or set its result. The command is trusted user input;
review it before use. Verification has a separate deadline and does not consume model tokens.
It does not run after a stopped/failed task or failed export. Verification containers are cleaned up.

`result.json` preserves `status` as a compatibility alias for execution state and adds:
- `execution_status`: `completed`, `stopped`, or `failed`.
- `verification_status`: `passed`, `failed`, `error`, or `not_run`.
- `verification`: configured command, exit code and bounded output, or the error/skipping reason.

`completed` only means execution ended normally. `passed` means the configured command exited zero,
not that all possible requirements are correct; command/test quality remains the evaluator's responsibility.
Without a configured verifier, the status is `not_run`, regardless of the model's success claims.
A verification failure or infrastructure error produces a nonzero CLI exit while preserving execution
status and the patch. For formal benchmarks, use their official independent tests and grader.
A fresh container and immutable tests do not prevent malicious candidate code from subverting an
in-process test runner; this is not a general adversarial grading guarantee.
