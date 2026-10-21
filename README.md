# Mini Agent

Mini Agent is a Python agent runtime with a terminal interface. The CLI runs each submitted task in a fresh Docker workspace copied from the source; it never mounts or edits the original repository. It works with OpenAI compatible and Anthropic compatible models, and covers tool calling, permission controls, long term memory, reusable Skills, context management, MCP integrations, and sub agents.

Use it to study agent architecture, run orchestration and evaluation experiments, or build agents for a specific workflow.

## Features

The sandbox CLI exposes the core coding tools, sub-agents, selected inline Skills and project-scoped
read-only memory, and opt-in guarded Context7 documentation queries. Other MCP integrations and
session recovery remain library capabilities and are disabled in the isolated CLI until adapted.

- **Agent loop.** Model request, tool call parsing, permission check, execution, result write back, repeat until done.
- **Dual protocol.** OpenAI compatible and Anthropic compatible endpoints.
- **Tool system.** Read, write, precise string replace edit, glob listing, grep search, shell, sub agents, and MCP tools.
- **Permission control.** Five modes (`plan`, `default`, `acceptEdits`, `dontAsk`, `bypassPermissions`), allow and deny rules, dangerous shell checks, read before edit, and stale-read checks (content hashes in sandbox mode, mtime in the local runtime). Skill forks can further restrict tools with an `allowed-tools` allowlist.
- **Tool call authorization.** Optional isolated approver (user request and proposed call only) and provenance ledger (arguments must match labeled values from the user request or structured tool results, or closed form derivations such as copy, difference, half split, and percent plus fee).
- **Skills.** Reusable methods stored as `SKILL.md`, retrieved by relevance and injected into the request. Includes examples such as `code_review` and read only `ops-troubleshooting`.
- **Skill self evolution.** Durable rules from explicit user feedback can be added or merged into a Skill, with records and version snapshots.
- **Long term memory.** Project scoped facts and preferences, isolated by project path hash. Recall is injected by the runtime; the model can call `compact_context` to fold a long session.
- **MCP integration.** Stdio JSON RPC client exposing external tools as `mcp__server__tool`.
- **Sub agents.** `explore`, `plan`, `general`, plus custom agents defined in Markdown.
- **Session recovery and compaction.** Auto saved sessions, `--resume`, and `/compact`.
- **CI.** GitHub Actions runs unit tests, AgentDojo adapter tests and real Docker isolation tests.

## Architecture

Modules and their responsibilities:

| Layer | Main files | Responsibility |
|-------|-----------|----------------|
| Entry | `agents/main.py`, `agents/sandbox_cli.py`, `agents/ui.py` | CLI options, one fresh isolated task per submission, terminal output |
| Execution | `agents/workspace.py`, `sandbox/worker.py` | Container lifecycle, snapshots, file/shell tools, validated change export |
| Harness | `agents/agent.py` | Agent loop, protocol adaptation, tool dispatch, compaction, session save |
| Capability | `agents/tools.py`, `agents/skills.py`, `agents/memory.py`, `agents/mcp_client.py`, `agents/subagent.py` | Tools, Skills, Memory, MCP, sub agents |
| Authorization | `agents/approval.py`, `agents/provenance.py` | Isolated and provenance approvers for side effecting tool calls |
| Prompt | `agents/prompt.py` | Dynamic system prompt construction |
| Evolution | `agents/online_skill_evolution.py`, `agents/skill_evolution.py`, `agents/online_skill_eval.py` | Skill extraction, persistence, audit, Skill quality evaluation |
| Persistence | `.miniagent/`, `~/.miniagent/` | Skills, Memory, sessions, audit artifacts, large tool results |

One sandbox task:

```text
user input
  -> snapshot selected source files (no host mounts)
  -> create restricted task container
  -> Agent.chat() on host, with model credentials on host
  -> model proposes a tool call
  -> host permission check
  -> file/shell tool executes inside the container (children share that container)
  -> result or denial returned to model, repeat
  -> export patch, changes and run report outside the source
  -> destroy task container
```

See [docs/architecture.md](docs/architecture.md) for the detailed design. Authorization and AgentDojo notes live under [tests/docs/](tests/docs/).

## Quick Start

Requires Python 3.11+, a running Docker engine, Git, and an OpenAI compatible or Anthropic compatible API endpoint. `ripgrep` is optional but makes search faster.

```bash
git clone <your-repo-url>
cd MiniAgent

python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

Create a `.env` file (see `.env.example`):

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

The execution container is non-root, has no host mounts, no network, a read-only root filesystem,
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
authorization with outcome `cancelled-while-waiting`. Sandbox children currently receive no approval
callback and deny operations requiring confirmation. Local library/evaluation callbacks are unchanged.

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

Legacy library features and API configuration are described in [docs/getting-started.md](docs/getting-started.md).

## Skills

A Skill is a reusable working method in Markdown: when to use it, how to approach the task, and what the output should look like. The runtime retrieves relevant Skills and injects them into the request.

```text
<project>/.miniagent/skills/<skill_name>/SKILL.md   # this project
~/.miniagent/skills/<skill_name>/SKILL.md           # your user directory (wins on name clash)
```

Shipped examples:

```text
.miniagent/skills/code_review/SKILL.md
.miniagent/skills/ops-troubleshooting/SKILL.md
```

`ops-troubleshooting` runs as a fork with a read tool allowlist for investigation without edits. Skills are optional. See [docs/skill-evolution.md](docs/skill-evolution.md).

## Evaluation

### Skill quality

Replay historical conversations against rules compiled from each `SKILL.md`, optionally with an LLM judge:

```bash
python3 -m agents.online_skill_eval
```

Or in the REPL: `/skill-eval`. Details: [docs/skills-evaluation.md](docs/skills-evaluation.md).

### AgentDojo (tool calling security and utility)

`evals/agentdojo.py` runs official AgentDojo suites through Mini Agent’s real permission and approval gate. It records per run:

- task success (`utility`, summarized as PNA on clean tasks)
- attack success rate (ASR)
- utility under attack and NRP
- latency, token usage, and estimated cost
- approval and audit counters

Install the pinned AgentDojo dependency, then for example:

```bash
pip install -r evals/requirements-agentdojo.txt
PYTHONPATH=. python -m evals.agentdojo --run --suite banking \
  --tasks user_task_0,user_task_1 \
  --injections injection_task_0 \
  --configs bypassPermissions:none:rules,default:isolated:mode-only,default:provenance:mode-only \
  --output artifacts/agentdojo
```

Scored JSON dumps under `evals/results/` and `artifacts/` stay local (gitignored). Narrative and limits: [tests/docs/agentdojo-evaluation.md](tests/docs/agentdojo-evaluation.md), [tests/docs/permissions-verification.md](tests/docs/permissions-verification.md).

### Automated tests and CI

```bash
PYTHONPATH=. python -m unittest discover -s tests -v
PYTHONPATH=. python -m unittest discover -s evals/tests -v
```

Real container tests (no LLM calls, temporary fixtures and fake credentials only):

```bash
MINIAGENT_DOCKER_TESTS=1 python -m unittest tests.test_workspace -v
```

GitHub Actions runs these container tests and the unit/adapter suites on push and pull request (`.github/workflows/ci.yml`). Offline permission demo: `python -m examples.permissions_demo`.

## Project Structure

```text
MiniAgent/
├── agents/
│   ├── main.py              # CLI entry, REPL
│   ├── agent.py             # agent loop, tool dispatch, session
│   ├── tools.py             # built in tools and permission modes
│   ├── approval.py          # isolated and provenance approvers
│   ├── provenance.py        # labeled values and sink matching
│   ├── prompt.py
│   ├── skills.py
│   ├── skill_evolution.py
│   ├── online_skill_evolution.py
│   ├── online_skill_eval.py
│   ├── memory.py
│   ├── mcp_client.py
│   ├── subagent.py
│   ├── session.py
│   ├── session_memory.py
│   ├── frontmatter.py
│   └── ui.py
├── evals/
│   ├── agentdojo.py         # AgentDojo adapter and runner
│   ├── requirements-agentdojo.txt
│   └── tests/               # adapter tests
├── tests/                   # unit and permission or provenance tests
│   └── docs/                # evaluation and authorization notes
├── examples/
│   └── permissions_demo.py
├── .github/workflows/
│   └── ci.yml
├── .miniagent/skills/
├── docs/
├── data/
├── Dockerfile
├── requirements.txt
└── .env.example
```

## Documentation

- [Getting Started](docs/getting-started.md)
- [Architecture](docs/architecture.md)
- [Source Code Guide](docs/source-code-guide.md)
- [Skill Self Evolution](docs/skill-evolution.md)
- [Skills Evaluation](docs/skills-evaluation.md)
- [AgentDojo evaluation notes](tests/docs/agentdojo-evaluation.md)
- [Permission verification notes](tests/docs/permissions-verification.md)
- [Authorization references](tests/docs/authorization-references.md)

## License

Original contributions that Penelope Liang has the right to license are
available under the [MIT License](LICENSE). This does not cover inherited
code or third-party materials, including `data/`.
The inherited code's licensing status remains unresolved.
