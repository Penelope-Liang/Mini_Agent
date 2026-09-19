# Mini Agent

Mini Agent is a Python agent runtime with a terminal interface. It works with OpenAI compatible and Anthropic compatible models, and covers tool calling, permission controls, long term memory, reusable Skills, context management, MCP integrations, and sub agents.

Use it to study agent architecture, run orchestration and evaluation experiments, or build agents for a specific workflow.

## Features

- **Agent loop.** Model request, tool call parsing, permission check, execution, result write back, repeat until done.
- **Dual protocol.** OpenAI compatible and Anthropic compatible endpoints.
- **Tool system.** Read, write, precise string replace edit, glob listing, grep search, shell, sub agents, and MCP tools.
- **Permission control.** Five modes (`plan`, `default`, `acceptEdits`, `dontAsk`, `bypassPermissions`), allow and deny rules, dangerous shell checks, read before edit, and mtime checks so a changed file must be re read before overwrite. Skill forks can further restrict tools with an `allowed-tools` allowlist.
- **Tool call authorization.** Optional isolated approver (user request and proposed call only) and provenance ledger (arguments must match labeled values from the user request or structured tool results, or closed form derivations such as copy, difference, half split, and percent plus fee).
- **Skills.** Reusable methods stored as `SKILL.md`, retrieved by relevance and injected into the request. Includes examples such as `code_review` and read only `ops-troubleshooting`.
- **Skill self evolution.** Durable rules from explicit user feedback can be added or merged into a Skill, with records and version snapshots.
- **Long term memory.** Project scoped facts and preferences, isolated by project path hash. Recall is injected by the runtime; the model can call `compact_context` to fold a long session.
- **MCP integration.** Stdio JSON RPC client exposing external tools as `mcp__server__tool`.
- **Sub agents.** `explore`, `plan`, `general`, plus custom agents defined in Markdown.
- **Session recovery and compaction.** Auto saved sessions, `--resume`, and `/compact`.
- **CI.** GitHub Actions runs unit tests and AgentDojo adapter tests on push and pull request.

## Architecture

Six layers, each with one job:

| Layer | Main files | Responsibility |
|-------|-----------|----------------|
| Entry | `agents/main.py`, `agents/ui.py` | Argument parsing, REPL, one shot runs, terminal output |
| Harness | `agents/agent.py` | Agent loop, protocol adaptation, tool dispatch, compaction, session save |
| Capability | `agents/tools.py`, `agents/skills.py`, `agents/memory.py`, `agents/mcp_client.py`, `agents/subagent.py` | Tools, Skills, Memory, MCP, sub agents |
| Authorization | `agents/approval.py`, `agents/provenance.py` | Isolated and provenance approvers for side effecting tool calls |
| Prompt | `agents/prompt.py` | Dynamic system prompt construction |
| Evolution | `agents/online_skill_evolution.py`, `agents/skill_evolution.py`, `agents/online_skill_eval.py` | Skill extraction, persistence, audit, Skill quality evaluation |
| Persistence | `.miniagent/`, `~/.miniagent/` | Skills, Memory, sessions, audit artifacts, large tool results |

One turn:

```text
user input
  -> Agent.chat()
  -> build prompt, retrieve Skills, prefetch Memory, init MCP
  -> call model (OpenAI compatible or Anthropic compatible)
  -> model returns text or a tool call
  -> permission check (and optional isolated or provenance approval)
  -> execute tool / Skill / MCP tool / sub agent
  -> write result back to the model
  -> repeat until the model stops calling tools
  -> save session, track Skill usage, run online skill evolution
```

See [docs/architecture.md](docs/architecture.md) for the detailed design. Authorization and AgentDojo notes live under [tests/docs/](tests/docs/).

## Quick Start

Requires Python 3.11+ and an OpenAI compatible or Anthropic compatible API endpoint. `ripgrep` is optional but makes search faster.

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

Start the interactive REPL:

```bash
python3 -m agents.main
```

Or run a single task and exit:

```bash
python3 -m agents.main "Summarize this project's directory structure and core modules"
```

Plan first, execute after approval:

```bash
python3 -m agents.main --plan "How should Skills retrieval be optimized?"
```

Full setup, environment variables, permission modes, and REPL commands are in [docs/getting-started.md](docs/getting-started.md).

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

GitHub Actions runs the same suites on push and pull request (`.github/workflows/ci.yml`). Offline permission demo: `python -m examples.permissions_demo`.

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
