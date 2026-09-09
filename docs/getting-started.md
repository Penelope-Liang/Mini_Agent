# Getting Started

This guide covers installation, configuration, running MiniAgent, permission modes, and the REPL commands.

## Requirements

- Python 3.11 or newer
- macOS or Linux
- An OpenAI-compatible or Anthropic-compatible API endpoint
- `ripgrep` (optional; search falls back to `grep`, then to a pure-Python scan)

## Install

```bash
git clone <your-repo-url>
cd MiniAgent

python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

Dependencies are intentionally few: `anthropic`, `openai`, `python-dotenv`, `rich`, `tqdm`.

## Configure

MiniAgent loads a `.env` file from the current directory or a parent directory. Copy `.env.example` and fill in your key.

### Anthropic-compatible endpoint

```env
APIKEY=sk-your-api-key
API=https://api.deepseek.com/anthropic
MODEL=claude-sonnet-4-6
```

### OpenAI-compatible endpoint

```env
OPENAI_API_KEY=sk-your-api-key
OPENAI_BASE_URL=https://your-host/v1
MODEL=gpt-4o
```

### Anthropic native

```env
ANTHROPIC_API_KEY=sk-ant-your-api-key
ANTHROPIC_BASE_URL=https://api.anthropic.com
MODEL=claude-sonnet-4-6
```

`ANTHROPIC_BASE_URL` is optional for the native API. The official host `api.anthropic.com` is recognized as Anthropic-compatible. Remove generic `API` and `APIKEY` settings when switching to this configuration.

### How the protocol is chosen

`agents/main.py::_resolve_api_config()` picks one base URL, then one protocol.

The base URL is the first of these that is set:

```text
--api-base  >  API  >  MINI_CLAUDE_API_BASE  >  OPENAI_BASE_URL  >  ANTHROPIC_BASE_URL
```

Then:

1. If that base URL's **hostname** is exactly `api.anthropic.com`, or its **path** ends with `/anthropic` or contains `/anthropic/`, the Anthropic-compatible client is used.
2. Otherwise, if any base URL was resolved, the OpenAI-compatible client is used.
3. If no base URL was resolved at all, an `ANTHROPIC_API_KEY` selects the Anthropic client and an `OPENAI_API_KEY` selects the OpenAI client.

The API key is resolved separately, and `APIKEY` / `MINI_CLAUDE_API_KEY` win over both `OPENAI_API_KEY` and `ANTHROPIC_API_KEY`. An API key is required — MiniAgent exits with an error if none is found.

Two consequences worth knowing:

- **Generic variables outrank the dedicated ones.** A leftover `API=https://old-host/anthropic` in your `.env` silently overrides `OPENAI_BASE_URL`, and a leftover `APIKEY=` overrides `OPENAI_API_KEY`. If a config change seems to have no effect, check for a stale `API` or `APIKEY` first.
- **Detection recognizes the official host and compatible paths.** `https://api.anthropic.com` selects Anthropic by hostname; `https://api.deepseek.com/anthropic` selects it by path. Other hosts without an Anthropic-compatible path use the OpenAI-compatible client.

## Run

### Interactive REPL

```bash
python3 -m agents.main
```

Then type a task, for example:

```text
Read this project and explain how the agent loop runs
```

Exit with `exit`, `quit`, or Ctrl-D. Ctrl-C once interrupts the current task; twice exits.

### One-shot

```bash
python3 -m agents.main "Summarize this project's directory structure and core modules"
```

### Resume the last session

```bash
python3 -m agents.main --resume
```

### Docker

The included `Dockerfile` installs Python, Node, ripgrep, and Playwright, and sets `python -m agents.main` as the entry point with `/workspace` as the working directory.

```bash
docker build -t miniagent .
docker run -it --rm \
  -v "$PWD:/workspace" \
  -v miniagent-home:/root/.miniagent \
  --env-file .env \
  miniagent
```

The second volume matters. Sessions, long-term memory, and user-level Skills live in `~/.miniagent/` *inside* the container, so without it `--rm` throws them away on exit and `--resume` has nothing to restore. Drop that volume only if you deliberately want a throwaway run.

## Command-line options

| Option | Effect |
|--------|--------|
| `--plan` | Plan mode: analyze and describe changes without executing them |
| `--accept-edits` | Auto-approve file edits, still confirm dangerous shell commands |
| `--yolo`, `-y` | Skip all confirmation prompts |
| `--dont-ask` | Auto-deny anything that would need confirmation (useful in CI) |
| `--thinking` | Enable extended thinking (Anthropic only) |
| `--model`, `-m` | Model to use; overrides `MODEL` |
| `--api-base` | Override the API base URL |
| `--resume` | Resume the last session |
| `--max-cost` | Stop when estimated spend exceeds this many USD |
| `--max-turns` | Stop after N agent turns |
| `--help`, `-h` | Show help |

## Permission modes

Every tool call is checked before it runs. Read-only tools (`read_file`, `list_files`, `grep_search`, `compact_context`) are always allowed.

| Mode | Flag | Behaviour |
|------|------|-----------|
| `default` | *(none)* | Ask before writing new files, editing missing files, dangerous shell commands, and Skill changes |
| `plan` | `--plan` | Block writes and shell, except for the plan file and anything matched by an `allow` rule |
| `acceptEdits` | `--accept-edits` | Auto-approve file edits; dangerous shell still needs confirmation |
| `bypassPermissions` | `--yolo` | Allow everything without asking, including tools matched by a `deny` rule |
| `dontAsk` | `--dont-ask` | Deny anything that would otherwise prompt |

Shell commands are screened against a list of dangerous patterns (`rm`, `sudo`, `git push`, `git reset`, `kill`, `shutdown`, and similar), which trigger a confirmation prompt outside of `bypassPermissions`.

MiniAgent also refuses to write or edit a file that has not been read first, and warns if a file changed on disk since it was last read.

### Permission rules in settings

You can pre-approve or block specific tools and paths:

```text
~/.miniagent/settings.json          # user-level
<project>/.miniagent/settings.json  # project-level
```

```json
{
  "permissions": {
    "allow": ["run_shell(npm test)", "read_file"],
    "deny": ["run_shell(git push*)"]
  }
}
```

Rules take the form `tool` or `tool(pattern)`. A trailing `*` matches by prefix. Patterns are matched against the shell command for `run_shell`, and against `file_path` for file tools.

### How rules interact with modes

`check_permission()` evaluates in this order:

```text
bypassPermissions?  -> allow, without consulting any rule
deny rules          -> deny
allow rules         -> allow
read-only tools     -> allow
plan mode           -> deny writes and shell
... remaining mode and confirmation logic
```

Two implications that are easy to get wrong:

- **An `allow` rule overrides Plan Mode.** Rules are checked before the mode is, so with `run_shell(npm test)` in your allow list, `--plan` will still run `npm test`. Plan Mode blocks everything you have *not* explicitly pre-approved — it is not an unconditional read-only sandbox.
- **`--yolo` ignores `deny` rules.** `bypassPermissions` returns before rules are loaded, so `deny: ["run_shell(git push*)"]` will not protect you there. Use `--accept-edits` if you want the deny list to stay in force.

Rules are cached on first use for the lifetime of the process, so editing a settings file mid-session has no effect until restart.

## REPL commands

| Command | Effect |
|---------|--------|
| `/clear` | Clear conversation history |
| `/plan` | Toggle plan mode |
| `/cost` | Show token usage and estimated cost |
| `/compact` | Compact the conversation into structured session memory |
| `/memory` | List saved long-term memories |
| `/skills` | List discovered Skills |
| `/skill-stats` | Show Skill usage and evolution stats |
| `/skill-eval` | Evaluate Skill quality, with the LLM judge enabled |
| `/extract_now [hint]` | Run Skill extraction on the current pending window |
| `/skill-feedback <skill> <rating> [note]` | Record feedback on a Skill |
| `/skill-evolve <skill> <lesson>` | Add a durable rule to a Skill |
| `/skill-create <name> \| <description> \| <when-to-use> \| <instructions>` | Create a Skill |
| `/<skill-name>` | Invoke a user-invocable Skill directly |

## Where data is stored

| Path | Contents |
|------|----------|
| `<project>/.miniagent/skills/` | Project-level Skills |
| `<project>/.miniagent/skill-evolution/` | Provenance, usage stats, evaluation artifacts |
| `<project>/.miniagent/rules/*.md` | Project rules injected into the system prompt |
| `<project>/.miniagent/agents/*.md` | Custom sub-agent definitions |
| `~/.miniagent/skills/` | User-level Skills, shared across projects |
| `~/.miniagent/projects/<hash>/memory/` | Long-term memory, isolated per project path |
| `~/.miniagent/sessions/` | Saved sessions for `--resume` |
| `~/.miniagent/plans/` | Plan Mode plan files |
| `~/.miniagent/tool-results/` | Persisted large tool outputs |
| `~/.miniagent/settings.json` | Permission rules and MCP servers |

Runtime directories are generated as you use MiniAgent and are excluded from version control.

## Skill self-evolution settings

Online Skill evolution is enabled by default. To be explicit:

```env
MINIAGENT_AUTO_SKILL_EVOLUTION=1
MINIAGENT_AUTO_SKILL_TARGET=project
```

`MINIAGENT_AUTO_SKILL_TARGET=user` writes new Skills to `~/.miniagent/skills/` instead, making them available in every project.

Because evolution writes files, it needs a permission mode that allows writes:

```bash
python3 -m agents.main --accept-edits
```

See [skill-evolution.md](skill-evolution.md) for what gets persisted and why.

## MCP servers

External MCP servers are configured in `.mcp.json` in the project root, or under `mcpServers` in a settings file:

```json
{
  "mcpServers": {
    "context7": { "command": "npx", "args": ["-y", "@upstash/context7-mcp"] }
  }
}
```

Their tools become available as `mcp__<server>__<tool>`.

## Next steps

- [Architecture](architecture.md) — how the runtime is put together
- [Source Code Guide](source-code-guide.md) — what to read first
