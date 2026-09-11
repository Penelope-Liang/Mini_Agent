# Mini Agent

Mini Agent is a AI agent runtime written in Python. It provides a terminal interface and supports OpenAI-compatible and Anthropic-compatible models, tool calling, permission controls, persistent memory, reusable Skills, context management, MCP integrations, and sub-agents.

The project provides a compact implementation of the main components used in an agent system. It can be used to study agent architecture, test orchestration and evaluation methods, or build agents for specific projects and workflows.

## Features

- **Agent loop** — model request, tool-call parsing, permission check, execution, result write-back, repeat until done.
- **Dual protocol** — works with OpenAI-compatible and Anthropic-compatible endpoints.
- **Tool system** — read, write, precise string-replace edit, glob listing, grep search, shell, sub-agents, and MCP tools.
- **Permission control** — five modes from fully interactive to unattended, plus allow/deny rules; Plan Mode blocks writes and shell apart from what you pre-approve.
- **Skills** — reusable working methods stored as `SKILL.md`, retrieved by relevance and injected into the request.
- **Skill self-evolution** — durable rules extracted from explicit user feedback, then added to or merged into a Skill, with provenance and version snapshots.
- **Long-term memory** — project-scoped facts, preferences, and decisions, isolated by project path hash.
- **MCP integration** — a stdio JSON-RPC client that exposes external MCP server tools as `mcp__server__tool`.
- **Sub-agents** — `explore`, `plan`, `general`, plus custom agents defined in Markdown.
- **Session recovery and compaction** — auto-saved sessions, `--resume`, and `/compact`.

## Architecture

Six layers, each with a single job:

| Layer | Main files | Responsibility |
|-------|-----------|----------------|
| Entry | `agents/main.py`, `agents/ui.py` | Argument parsing, REPL, one-shot runs, terminal output |
| Harness | `agents/agent.py` | Agent loop, protocol adaptation, tool dispatch, compaction, session save |
| Capability | `agents/tools.py`, `agents/skills.py`, `agents/memory.py`, `agents/mcp_client.py`, `agents/subagent.py` | Tools, Skills, Memory, MCP, sub-agents |
| Prompt | `agents/prompt.py` | Dynamic system prompt construction |
| Evolution | `agents/online_skill_evolution.py`, `agents/skill_evolution.py`, `agents/online_skill_eval.py` | Skill extraction, persistence, audit, evaluation |
| Persistence | `.miniagent/`, `~/.miniagent/` | Skills, Memory, sessions, audit artifacts, large tool results |

One turn looks like this:

```text
user input
  -> Agent.chat()
  -> build prompt, retrieve Skills, prefetch Memory, init MCP
  -> call model (OpenAI-compatible or Anthropic-compatible)
  -> model returns text or a tool call
  -> permission check
  -> execute tool / Skill / MCP tool / sub-agent
  -> write result back to the model
  -> repeat until the model stops calling tools
  -> save session, track Skill usage, run online skill evolution
```

See [docs/architecture.md](docs/architecture.md) for the detailed design.

## Quick Start

Requires Python 3.11+, and an OpenAI-compatible or Anthropic-compatible API endpoint. `ripgrep` is optional but makes search faster.

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

A Skill is a reusable working method written in Markdown: when to use it, how to approach the task, what the output should look like. The runtime retrieves relevant Skills and injects them into the request, so the agent follows a known-good method instead of improvising.

Skills are discovered from directories — one directory per Skill, containing a file named exactly `SKILL.md`:

```text
<project>/.miniagent/skills/<skill_name>/SKILL.md   # project-level
~/.miniagent/skills/<skill_name>/SKILL.md           # user-level, takes priority
```

This repository ships one example, `code_review`:

```text
.miniagent/skills/code_review/SKILL.md
```

Skills are optional — the runtime works with none installed. Adding one requires no code changes.

Mini Agent can also refine Skills from your feedback. When you state a durable rule (not a one-off request), it can add a new Skill or merge the rule into an existing one, recording where the change came from. See [docs/skill-evolution.md](docs/skill-evolution.md).

## Evaluation

Mini Agent includes a built-in evaluation pipeline for **Skill quality**: it replays historical conversations against the rules compiled from each `SKILL.md`, optionally using an LLM judge, and reports whether a Skill is actually being retrieved and used.

```bash
python3 -m agents.online_skill_eval   # programmatic rules only
```

Or inside the REPL, which also enables the LLM judge:

```text
/skill-eval
```

This measures Skills, not end-to-end task success. It is not a task benchmark such as GAIA or HLE, and this repository does not currently ship a harness for those. Details in [docs/skills-evaluation.md](docs/skills-evaluation.md).

## Project Structure

```text
MiniAgent/
├── agents/                 # the runtime
│   ├── main.py             # CLI entry, REPL, argument parsing
│   ├── agent.py            # agent loop, tool dispatch, compaction, session save
│   ├── tools.py            # built-in tools and the permission system
│   ├── prompt.py           # dynamic system prompt construction
│   ├── skills.py           # Skill discovery, retrieval, execution
│   ├── skill_evolution.py  # Skill persistence, version snapshots, audit
│   ├── online_skill_evolution.py  # candidate extraction, add/merge/discard
│   ├── online_skill_eval.py       # Skill quality evaluation
│   ├── memory.py           # project-scoped long-term memory
│   ├── mcp_client.py       # MCP stdio JSON-RPC client
│   ├── subagent.py         # sub-agent configuration
│   ├── session.py          # session save and recovery
│   ├── session_memory.py   # structured session memory folding
│   ├── frontmatter.py      # SKILL.md frontmatter parsing
│   └── ui.py               # terminal output
├── .miniagent/
│   └── skills/             # project-level Skills
├── docs/                   # documentation
├── data/                   # third-party evaluation datasets
├── Dockerfile
├── requirements.txt
└── .env.example
```

## Documentation

- [Getting Started](docs/getting-started.md) — install, configure, run, permission modes, REPL commands
- [Architecture](docs/architecture.md) — layers, agent loop, tools, Skills, Memory, MCP, sub-agents
- [Source Code Guide](docs/source-code-guide.md) — a reading order through the codebase
- [Skill Self-Evolution](docs/skill-evolution.md) — how Skills are extracted, merged, and audited
- [Skills Evaluation](docs/skills-evaluation.md) — how Skill quality is measured

## License

Original contributions that Penelope Liang has the right to license are
available under the [MIT License](LICENSE). This does not cover inherited
code or third-party materials, including `data/`.
The inherited code's licensing status remains unresolved.
