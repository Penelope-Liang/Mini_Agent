# Source Code Guide

A reading order through the codebase, main path first.

## The core flow

```text
user input
  -> main.py parses args and mode
  -> Agent.chat() enters one conversation turn
  -> build prompt / retrieve Skills / prefetch Memory / initialize MCP
  -> model returns a tool call
  -> runtime performs permission checks
  -> execute tools, Skills, MCP tools, or sub-agents
  -> write tool results back to the model
  -> compact_context when needed
  -> save session
  -> trigger Skill usage stats and self-evolution in the background
```

Four things worth understanding by the end:

1. How one request enters the agent loop.
2. How tools, permissions, and context are governed by the runtime.
3. How Skills, Memory, MCP, and sub-agents plug in.
4. How self-evolution and evaluation close the loop.

## Reading order

1. `agents/main.py`
2. `agents/agent.py`
3. `agents/tools.py`
4. `agents/prompt.py`
5. `agents/skills.py`
6. `agents/online_skill_evolution.py`
7. `agents/skill_evolution.py`
8. `agents/online_skill_eval.py`
9. `agents/memory.py`
10. `agents/mcp_client.py`
11. `agents/subagent.py`
12. `agents/session.py`, `agents/session_memory.py`
13. `agents/ui.py`, `agents/frontmatter.py`

The principle: main path before support modules, runtime before persistence, how it runs before how it remembers.

## 1. `agents/main.py`

The entry layer. It turns command-line arguments and environment variables into Agent configuration — it does not run the agent itself.

Focus on:

- how CLI arguments are parsed;
- how `--plan`, `--resume`, `--accept-edits`, `--yolo`, `--dont-ask` map to permission modes;
- how `API` / `APIKEY`, `OPENAI_*`, `ANTHROPIC_*` decide the model interface;
- how REPL and one-shot are routed;
- what `--resume` restores.

Start with `parse_args()`, `_resolve_permission_mode()`, `_resolve_api_config()`, `run_repl()`, `run_one_shot()`, `main()`.

After reading you should know where the model comes from, how the permission mode is chosen, when the REPL runs versus a one-shot task, and what session state `--resume` brings back.

## 2. `agents/agent.py`

The core runtime.

### Start with `Agent.__init__()`

The fields are effectively the runtime's state diagram:

- `permission_mode`, `model`, `use_openai`, `tools`
- `_mcp_manager`
- `_anthropic_messages` / `_openai_messages`
- `_read_file_state`
- `_already_surfaced_memories`
- `_pending_skill_extraction_window`
- `_background_skill_tasks`
- `_folded_session_memories`
- `_base_system_prompt` / `_system_prompt`

### Then `Agent.chat()`

The main entry for each turn:

```text
chat(user_message)
  -> initialize MCP
  -> merge the previous pending window
  -> inject Skills context
  -> choose the OpenAI or Anthropic loop
  -> wait for model output and the tool loop
  -> schedule usage tracking
  -> schedule online skill evolution
  -> record a new pending window
  -> auto-save the session
```

Questions to answer here: why one turn is not one model call; why there are background tasks; why there is a pending window; why the session is auto-saved.

### The router: `_execute_tool_call()`

This is where an already-approved tool call gets dispatched — the permission gate sits upstream of it, in the chat loop. Read it as a routing table:

| Tool | Goes to |
|------|---------|
| `compact_context` | session folding |
| `enter_plan_mode` / `exit_plan_mode` | plan mode switch |
| `agent` | sub-agent |
| `skill` | Skill invocation |
| `mcp__...` | MCP tool |
| everything else | `agents/tools.py` |

### Then the context helpers

`_run_compression_pipeline()`, `_check_and_compact()`, `_compact_conversation()`, `_persist_large_result()`, `_auto_save()`, `restore_session()`.

These explain how sessions are compacted, how large results are persisted, and why `--resume` restores the conversation but not the full working state.

## 3. `agents/tools.py`

Answers "what can the model do, and when is it allowed to?"

### Tool definitions

`tool_definitions` contains `read_file`, `write_file`, `edit_file`, `list_files`, `grep_search`, `run_shell`, `skill`, `compact_context`, `skill_create`, `skill_evolve`, `enter_plan_mode`, `exit_plan_mode`, `agent`, `tool_search`.

### Permissions

Read `check_permission()` and `_check_permission_rules()`. The hard boundaries:

- read-only tools are allowed directly;
- settings rules are consulted *before* the mode, so an `allow` rule wins over Plan Mode, and `bypassPermissions` returns before `deny` rules are even loaded;
- Plan Mode otherwise blocks edits and shell;
- dangerous shell requires confirmation;
- new files, Skill creation, and Skill evolution require confirmation;
- `dontAsk` rejects anything that would prompt.

### Read-before-edit enforcement

MiniAgent does not rely on the prompt saying "read before editing." It enforces it: `read_file` records path and mtime, edits check that state, and an externally modified file must be read again.

## 4. `agents/prompt.py`

Assembles runtime context into the system prompt: project rules, Memory instructions, the Skills list, sub-agent types, deferred tools, working directory, and Git status. The prompt is runtime state, not fixed text.

## 5. `agents/skills.py`

Skill discovery, retrieval, execution, and caching.

### `discover_skills()`

Scans user-level `~/.miniagent/skills/` and project-level `<cwd>/.miniagent/skills/`. Understand why user-level takes priority, why project-level does not override a same-named user Skill, and how `SKILL.md` becomes a `SkillDefinition`.

Note that discovery is directory-based: a Skill is a directory containing a file named exactly `SKILL.md`. A loose `skills/foo.md` is ignored.

### `retrieve_relevant_skills()`

Lightweight lexical retrieval, not vector recall. Understand why `name`, `description`, and `when_to_use` weigh more than body text, why body text still participates, and why matches are injected as `<retrieved_skills>`.

### `execute_skill()`

Decides whether a Skill is injected into the current context (`inline`) or run in a sub-agent (`fork`). A Skill is a reusable task method, not just prompt text.

## 6. `agents/online_skill_evolution.py`

The self-evolution entry point. Read in this order:

1. `extract_online_skill_candidate()`
2. `maintain_online_skill_candidate()`
3. `online_ingest()`

The interesting part is the flow logic, not the JSON schema: next-turn feedback is the evidence, the extractor proposes at most one candidate, the maintainer decides add / merge / discard, one-off content must not become long-term capability, and every result writes provenance.

## 7. `agents/skill_evolution.py`

Persistence, versioning, and audit: `create_skill_file()`, `evolve_skill_file()`, `record_online_skill_provenance()`, `record_skill_invocation()`, `record_skill_usage_judgments()`.

Understand where Skill files are written, how versions bump, how old versions are archived, how provenance and usage stats are recorded, and why evolution does not silently overwrite an active Skill.

## 8. `agents/online_skill_eval.py`

Skill quality evaluation, in four layers:

```text
provenance
  -> replay pool
  -> rules / LLM judge
  -> candidate variants / champion
```

Focus on `evaluate_online_skill_evolution()`, `evaluate_online_skill_evolution_async()`, `_build_replay_pool()`, `_compile_eval_rules()`, `_evaluate_rule_async()`, `_build_candidate_eval_bundle_async()`, `_set_champion()`.

Understand why evaluation replays historical samples rather than inspecting a single file, why the active Skill and its candidates are kept separate, and why a champion is only a local record of a healthy version.

## 9. `agents/memory.py`

File-based long-term memory: `get_memory_dir()`, `save_memory()`, `list_memories()`, `scan_memory_headers()`, `select_relevant_memories()`, `start_memory_prefetch()`.

Understand that Memory is not session memory — it is cross-session and project-scoped; that headers are scanned before bodies are read; and that injection has a budget.

## 10. `agents/mcp_client.py`

MCP integration via `McpConnection` and `McpManager`: read config, start the server, initialize, fetch `tools/list`, wrap external tools as agent tools, and route calls through the `mcp__server__tool` naming scheme.

## 11. `agents/subagent.py`

Context isolation. `explore` is read-only search, `plan` is read-only planning, `general` runs a self-contained task. Custom agents load from `.miniagent/agents/*.md`, and `allowed-tools` narrows their tool scope.

## 12. Support modules

Read these last. They do not define the main line, but they make the project runnable, persistable, and replayable.

- `session.py` — session save and recovery
- `session_memory.py` — structured folding of session state
- `ui.py` — terminal output, tool call rendering, sub-agent labels
- `frontmatter.py` — Markdown frontmatter parsing and generation

## Overview

```text
main.py
  -> Agent.chat()
    -> prompt.py
    -> memory.py
    -> skills.py
    -> mcp_client.py
    -> subagent.py
    -> tools.py
    -> online_skill_evolution.py
    -> skill_evolution.py
    -> online_skill_eval.py
    -> session.py / session_memory.py
```
