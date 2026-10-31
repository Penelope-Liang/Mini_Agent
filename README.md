# Mini Agent

Mini Agent is a coding agent that explores repositories, edits files, and runs commands in isolated Docker workspaces. The agent combines tool permission controls, loop detection, and optional independent verification to make agent execution inspectable and testable.

Supports OpenAI-compatible and Anthropic-compatible models through a terminal interface.

## Features

- **Isolated execution.** Each task works on a copy of the repository in a fresh Docker container, with networking disabled and resource limits. Changes are exported for review.
- **Permission controls.** File edits and commands pass through a host-side permission gate. Evaluation integrations also support isolated approval and argument provenance checks.
- **Loop detection.** Detects repeated tool calls, recurring failures, and execution cycles, and stops tasks that are no longer making progress.
- **Independent verification.** An optional, user-configured command checks exported changes in a fresh container. Acceptance files can be protected from modification.
- **Execution records.** Saves tool audit logs, transcripts, task duration, token usage, and estimated model cost.

The sandbox CLI also supports sub-agents, explicitly selected Skills, read-only project memory, and opt-in Context7 documentation queries. See the [sandbox guide](docs/sandbox-cli.md) for supported options; broader MCP integration and session recovery are library capabilities.

## Quick Start

Requires Python 3.13, Git, a running Docker engine, and a model API key.

```bash
git clone https://github.com/Penelope-Liang/Mini_Agent.git
cd Mini_Agent

python3.13 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt

docker build -t miniagent-sandbox:1 sandbox
```

Create a `.env` file in this directory. For OpenAI:

```env
OPENAI_API_KEY=your-api-key
OPENAI_BASE_URL=https://api.openai.com/v1
MODEL=gpt-4o
```

For other providers, see [model configuration](docs/getting-started.md#configure). Remove generic `API` and `APIKEY` settings when switching to the OpenAI configuration above, since they take precedence.

Run a task against a repository:

```bash
python3 -m agents.main --workspace /path/to/repo "Fix the input validation bug"
```

Or start interactive mode:

```bash
python3 -m agents.main --workspace /path/to/repo
```

Each submitted task gets a fresh context and container. The original repository is not modified. Outputs are saved under `~/.miniagent/sandbox-runs/<task>/`:

- `changes.patch` and `changes.json` — exported changes for review.
- `audit.jsonl` — tool permission and execution records.
- `result.json` — execution status, transcript, token usage, estimated cost, and duration.

The default image contains Python and shell utilities. Projects needing additional dependencies require a prepared image; see [sandbox setup and execution](docs/sandbox-cli.md#setup-and-execution).

To check changes independently, supply `--verify-command`; without it, verification is recorded as `not_run`. See [verification setup](docs/sandbox-cli.md#protected-acceptance-tests-and-independent-verification).

## Documentation

- [Sandbox CLI](docs/sandbox-cli.md) — approvals, plan mode, Skills, memory, Context7, and verification.
- [Configuration and library runtime](docs/getting-started.md) — providers and retained library features.
- [Architecture](docs/architecture.md) — components and execution flow.
- [Source code guide](docs/source-code-guide.md) — implementation walkthrough.
- [Testing and evaluation](docs/testing.md) — unit tests, Docker checks, and AgentDojo evaluation.
- [Skill evolution](docs/skill-evolution.md) — reusable instructions and feedback workflows.

## License

Original contributions that Penelope Liang has the right to license are
available under the [MIT License](LICENSE). This does not cover inherited
code or third-party materials, including `data/`.
The inherited code's licensing status remains unresolved.
