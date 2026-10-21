# Architecture

> **Sandbox CLI:** `python -m agents.main` now creates a fresh Docker workspace per task and
> exports changes outside the original repository. Build with
> `docker build -t miniagent-sandbox:1 sandbox`. See the [current quick start](../README.md#quick-start)
> for supported flags, snapshot exclusions, limits and results. The Skills/MCP/Memory and persistent
> REPL descriptions below document the retained library runtime, not the isolated CLI.


MiniAgent's core is the harness — the layer around the model.

The model reasons and proposes tool-call intent. The runtime owns the real execution boundaries: permission checks, tool execution, result write-back, context management, session persistence, and Skill accumulation.

A plain chat tool is:

```text
user input -> call model -> print reply
```

MiniAgent is:

```text
user input
  -> CLI / REPL
  -> harness
  -> prompt / Skills / Memory / MCP initialization
  -> model reasoning
  -> tool call
  -> permission check
  -> tool execution
  -> tool result write-back
  -> multi-turn loop
  -> session save
  -> Skill usage tracking
  -> online skill evolution
```

## Layers

| Layer | Main files | Responsibility |
|-------|-----------|----------------|
| Entry | `agents/main.py`, `agents/ui.py` | Argument parsing, REPL, one-shot runs, terminal output |
| Harness | `agents/agent.py` | Agent loop, protocol adaptation, tool dispatch, compaction, session save |
| Capability | `agents/tools.py`, `agents/skills.py`, `agents/memory.py`, `agents/mcp_client.py`, `agents/subagent.py` | Tools, Skills, Memory, MCP, sub-agents |
| Prompt | `agents/prompt.py` | Dynamic system prompt construction |
| Evolution | `agents/online_skill_evolution.py`, `agents/skill_evolution.py`, `agents/online_skill_eval.py` | Skill extraction, persistence, audit, evaluation |
| Persistence | `.miniagent/`, `~/.miniagent/` | Skills, Memory, sessions, audit artifacts, large results |

The boundaries are deliberately narrow:

- `main.py` handles entry only.
- `agent.py` handles runtime dispatch only.
- `tools.py` handles tools and permissions only.
- `skills.py` and `skill_evolution.py` handle capability persistence only.
- `memory.py` handles long-term facts only.
- `mcp_client.py` handles external tool integration only.
- `subagent.py` handles isolated-context tasks only.

## Request pipeline

Input enters through `agents/main.py` and lands in `Agent.chat()`:

```text
user input
  -> run_repl() / run_one_shot()
  -> Agent.chat(user_message)
  -> initialize MCP on first chat
  -> retrieve Skills and inject into user message
  -> prefetch Memory asynchronously
  -> _chat_openai() or _chat_anthropic()
  -> model returns text or tool call
  -> check_permission()
  -> _execute_tool_call() dispatches and runs the tool
  -> write tool result back to the model
  -> model continues reasoning or finishes
  -> background Skill usage tracking / online evolution
  -> save pending extraction window
  -> auto-save session
```

Three properties matter here:

1. The model never touches the environment directly.
2. Permission checks happen before execution, not after.
3. Capability persistence happens after the task ends, off the critical path.

## Entry layer

`agents/main.py` parses arguments, loads `.env`, decides the model protocol, constructs the `Agent`, and runs either the REPL or a one-shot task. It also handles REPL commands such as `/skills`, `/memory`, `/compact`, `/plan`, and the `/skill-*` family.

| Function | Role |
|----------|------|
| `parse_args()` | Parse model, permission flags, plan mode, resume |
| `_load_env_file()` | Load `.env` from the current or a parent directory |
| `_resolve_api_config()` | Decide OpenAI-compatible vs Anthropic-compatible |
| `run_repl()` | Interactive main loop |
| `run_one_shot()` | Single-task execution |

The entry layer knows nothing about tool internals or how Skills evolve. It only assembles configuration and hands input to the runtime.

## Harness layer

`agents/agent.py` is the core module. State held by `Agent`:

| State | Meaning |
|-------|---------|
| `permission_mode` | Current permission mode |
| `model` | Current model name |
| `use_openai` | Whether the OpenAI-compatible path is active |
| `tools` | Currently available tool list |
| `_anthropic_messages` / `_openai_messages` | Dual-protocol message history |
| `_mcp_manager` | MCP manager |
| `_last_retrieved_skill_reference` | Skills retrieved this turn |
| `_pending_skill_extraction_window` | Window awaiting the next round of feedback |
| `_background_skill_tasks` | Background usage-tracking and evolution tasks |

`Agent.chat()` delegates to:

| Method | Responsibility |
|--------|----------------|
| `_augment_user_message_with_skill_context()` | Retrieve relevant Skills and inject them |
| `_chat_openai()` / `_chat_anthropic()` | Protocol-specific main loops |
| `_execute_tool_call()` | Unified tool-call dispatch |
| `_execute_skill_tool()` | Execute a Skill |
| `_execute_agent_tool()` | Launch a sub-agent |
| `_run_online_skill_evolution()` | Background online self-evolution |
| `_run_skill_usage_tracking()` | Judge whether retrieved Skills were actually used |
| `_run_compression_pipeline()` | Control long context and large results |
| `_auto_save()` | Save the session |

## Model protocol adaptation

Protocol detection lives in `agents/main.py::_resolve_api_config()`; the runtime then branches:

```text
if self.use_openai:
    self._chat_openai(user_message)
else:
    self._chat_anthropic(user_message)
```

| Difference | OpenAI-compatible | Anthropic-compatible |
|------------|-------------------|----------------------|
| Client | `openai.AsyncOpenAI` | `anthropic.AsyncAnthropic` |
| Message container | `_openai_messages` | `_anthropic_messages` |
| Tool call format | `tool_calls` | `tool_use` block |
| Result write-back | `tool` role message | `tool_result` block |

Everything else is shared: tools, permissions, Skills, Memory, sessions, and the evolution pipeline. Swapping model providers does not require touching the tool system.

## Tool system and permissions

Tools are defined in `agents/tools.py` as `tool_definitions`:

`read_file`, `write_file`, `edit_file`, `list_files`, `grep_search`, `run_shell`, `skill`, `compact_context`, `skill_evolve`, `skill_create`, `agent`, `tool_search`, plus the deferred `enter_plan_mode` and `exit_plan_mode`.

```text
model initiates tool call
  -> check_permission() in the chat loop
  -> deny: return the reason as the tool result, never execute
  -> confirm: ask the user, then proceed or abort
  -> allow: Agent._execute_tool_call()
       -> Agent-layer tools routed first (compact_context, plan mode, agent, skill)
       -> MCP tools routed to McpManager
       -> everything else enters tools.execute_tool()
  -> result formatted and truncated if large
  -> written back to the model
```

The permission gate lives in the chat loops (`agents/agent.py`), not inside `execute_tool()` — `tools.execute_tool()` never calls `check_permission` itself. Every path goes through the gate first, including the speculative one: when a concurrency-safe tool (`read_file`, `list_files`, `grep_search`) is started early during streaming to hide latency, its permission is checked before the task is created.

`check_permission()` evaluates in a fixed order, and the order is load-bearing:

```text
bypassPermissions?  -> allow, without consulting any rule
deny rules          -> deny
allow rules         -> allow
read-only tools     -> allow
plan mode           -> deny writes and shell (except the plan file)
acceptEdits         -> allow edit-class tools
dangerous / new-file / skill-write cases -> confirm, or deny under dontAsk
otherwise           -> allow
```

Because settings rules are consulted before the mode, an `allow` rule wins over Plan Mode, and `bypassPermissions` short-circuits ahead of `deny` rules. Plan Mode is therefore a strong default, not an unconditional read-only sandbox; see [getting-started.md](getting-started.md#how-rules-interact-with-modes).

Additional protections:

- Dangerous shell patterns require confirmation.
- Writing a new file requires confirmation.
- A file must be read before it can be written or edited.
- If a file changed on disk since the last read, the agent is told to read it again.

The goal is not to stop the agent from working, but to make risky actions explicit.

### Deferred tools

Some tools are marked `deferred` and hidden from the model until needed. `tool_search` looks them up by name or keyword and activates them, which keeps the default tool list small without permanently losing capability.

## Skills

Skills carry reusable working methods — a review workflow, a document structure, the steps for a domain task. They live as directory-style `SKILL.md` files:

```text
<project>/.miniagent/skills/<skill_name>/SKILL.md
~/.miniagent/skills/<skill_name>/SKILL.md
```

Discovery is directory-based only: each Skill is a directory containing a file named exactly `SKILL.md`. User-level Skills take priority over project-level ones with the same name.

| File | Responsibility |
|------|----------------|
| `agents/skills.py` | Load, parse, retrieve, and execute Skills |
| `agents/skill_evolution.py` | Create, evolve, snapshot, provenance, usage stats |
| `agents/online_skill_evolution.py` | Online candidate extraction and add/merge/discard decisions |

Three runtime phases:

```text
before the model call
  -> discover_skills()
  -> retrieve_relevant_skills()
  -> format_retrieved_skill_context()
  -> inject into the user message

during the conversation
  -> model calls the skill tool
  -> inline or fork execution

after the conversation
  -> judge_retrieved_skill_usage()
  -> online_ingest()
  -> create_skill_file() or evolve_skill_file()
```

A Skill can run `inline` (its instructions are injected into the current context) or `fork` (it runs in a sub-agent). Only `fork` Skills honour an `allowed-tools` whitelist.

## Self-evolution

Self-evolution does not let the agent rewrite its own prompts freely. It persists stable, reusable methods derived from explicit user feedback.

```text
turn N: user task -> agent output -> save pending window
turn N+1: user feedback
  -> merge into the previous window
  -> Extractor proposes a candidate Skill
  -> Maintainer decides add / merge / discard
  -> create_skill_file() or evolve_skill_file()
  -> record provenance, usage stats, version snapshot
```

| Capability | Description |
|------------|-------------|
| `create_skill_file()` | Create a new `SKILL.md` |
| `evolve_skill_file()` | Evolve an existing Skill and snapshot the previous version |
| `record_online_skill_provenance()` | Record the evidence behind a change |
| `record_skill_usage_judgments()` | Record retrieved / relevant / used statistics |
| `format_skill_stats()` | Report whether Skills are actually earning their place |

See [skill-evolution.md](skill-evolution.md) for the full pipeline.

## Memory

Memory and Skills store different things:

| Type | Stores | Examples |
|------|--------|----------|
| Memory | Facts, preferences, background, past decisions | This project deploys with Docker; a given API is deprecated |
| Skills | Methods, workflows, style, reusable steps | Review output structure; refactor checklist |

Implemented in `agents/memory.py`:

```text
save
  -> save_memory()
  -> write to ~/.miniagent/projects/<project_hash>/memory/
  -> update index

recall
  -> scan_memory_headers()
  -> start_memory_prefetch()
  -> select_relevant_memories()
  -> format_memories_for_injection()
```

Memory is isolated by a hash of the project path, so projects do not contaminate each other.

## MCP integration

MCP brings external tools into the agent. MiniAgent does not depend on an MCP SDK; `agents/mcp_client.py` implements a stdio JSON-RPC client directly.

| Object | Responsibility |
|--------|----------------|
| `McpConnection` | One MCP server subprocess and its JSON-RPC traffic |
| `McpManager` | Read config, start servers, merge tool definitions, route calls |

```text
first Agent.chat()
  -> McpManager.load_and_connect()
  -> read user / project config
  -> start MCP server
  -> initialize
  -> tools/list
  -> wrap as mcp__server__tool
  -> merge into Agent.tools
```

Calls are routed by `McpManager.is_mcp_tool()` and executed via `tools/call`.

## Sub-agents

Sub-agents run isolated tasks so the main context stays clean. The model calls the `agent` tool; `_execute_agent_tool()` builds the sub-agent from `get_sub_agent_config()`, runs it independently, and returns only its result.

| Type | Tool scope | Use case |
|------|-----------|----------|
| `explore` | Read-only tools | Locate code, search quickly |
| `plan` | Read-only tools | Produce a structured plan |
| `general` | All tools except `agent` | Handle a self-contained task |

Custom sub-agents can be defined as Markdown files in `.miniagent/agents/*.md`, with an `allowed-tools` whitelist in frontmatter. Sub-agents cannot call `agent`, which prevents unbounded recursion.

## Prompt construction

The system prompt is runtime state, not fixed text. `agents/prompt.py::build_system_prompt()` assembles:

- base identity and working rules;
- current working directory;
- Git context;
- project rules from `.miniagent/rules/*.md`;
- Memory instructions and index;
- available Skills;
- available sub-agents;
- hints about deferred tools.

## Sessions, compaction, and large results

Long tasks produce many messages and large tool outputs, so context volume has to be managed.

| Mechanism | Description |
|-----------|-------------|
| `save_session()` / `load_session()` | Persist and restore a session |
| `_run_compression_pipeline()` | Compress messages and tool results |
| `compact_context` tool | Let the model fold context on purpose |
| `_persist_large_result()` | Write a large tool result to disk, keep only a reference in context |

Compaction folds history into structured session memory — episode, working, and tool layers — rather than truncating blindly, so task progress, key evidence, and next steps survive.

## Storage

| Data | Path |
|------|------|
| Project Skills | `.miniagent/skills/<skill_name>/SKILL.md` |
| User Skills | `~/.miniagent/skills/<skill_name>/SKILL.md` |
| Skill evolution audit | `.miniagent/skill-evolution/` |
| Memory | `~/.miniagent/projects/<project_hash>/memory/` |
| Sessions | `~/.miniagent/sessions/` |
| Large tool results | `~/.miniagent/tool-results/` |
| Plan files | `~/.miniagent/plans/` |
| MCP and permission config | `.mcp.json`, `~/.miniagent/settings.json`, `.miniagent/settings.json` |

Two principles: project-specific capability lives in the project directory; user preferences and cross-project capability live in the home directory.

## Where to look

| Question | File |
|----------|------|
| How does input enter the system? | `agents/main.py` |
| How does one request run the loop? | `agents/agent.py` |
| How does the model call tools? | `agents/agent.py::_execute_tool_call()` |
| How are tools executed? | `agents/tools.py` |
| How are permissions decided? | `agents/tools.py::check_permission()` |
| How is the system prompt built? | `agents/prompt.py` |
| How are Skills loaded and retrieved? | `agents/skills.py` |
| How are Skills added or merged? | `agents/online_skill_evolution.py`, `agents/skill_evolution.py` |
| How is Memory saved and recalled? | `agents/memory.py` |
| How is MCP integrated? | `agents/mcp_client.py` |
| How are sub-agents created? | `agents/subagent.py`, `agents/agent.py::_execute_agent_tool()` |
| How are sessions saved and restored? | `agents/session.py`, `agents/agent.py` |

## Design boundaries

MiniAgent is a general runtime, not a vertical application. "General" means it does not assume one scenario. "Runtime" means it genuinely controls execution boundaries instead of letting the model act on the environment directly.

What it deliberately does not do:

- It does not let the model rewrite its own instructions without evidence and an audit trail.
- It does not hide capability behind a plugin framework; tools and Skills are plain definitions and Markdown files.
- It does not ship a task benchmark harness; see [skills-evaluation.md](skills-evaluation.md) for what is actually measured.


## Task-local stuck detection

`agents/stuck_detector.py` implements bounded-pattern stopping independently of the framework,
with OpenHands [StuckDetector](https://github.com/OpenHands/software-agent-sdk/blob/main/openhands-sdk/openhands/sdk/conversation/stuck_detector.py)
as an architectural reference. This is a repetition heuristic, not proof that a task is unsolvable.

Each Agent accepts `stuck_warn_at=3` and `stuck_stop_at=4` (positive integers, warning strictly before
stopping). The third matching observation appends a corrective warning to the tool result; the fourth
sets a stop reason and prevents further tool/model dispatch. These thresholds are inherited by both
general sub-agents and Skill forks. Detection resets on a new `chat` task, never on context compaction.
Existing tool statistics remain cumulative telemetry and do not control stopping.

Three signals are checked:

- Identical tool name, canonical JSON arguments, complete result fingerprint, and edit version.
- Consecutive errors from the same call and version, even when error messages differ.
- Permission denials accumulated within the task, separately from ordinary progress. Identical calls
  use the normal thresholds; per-tool denial totals use twice those thresholds (6/8 by default) to
  catch argument variations. Explicit human approval clears the matching call and that tool's denial
  total. Denials of other calls remain recorded. Simple unquoted shell whitespace is normalized;
  quoted strings, escapes and newlines retain their original semantics.

Results carry `success`, `error` or `denied` metadata. Permission boundaries mark rejection explicitly;
container shell status comes from the exit code, and file failures from worker errors. Older local
string-only handlers use a compatibility adapter for their known error prefixes, not arbitrary
substring matching of returned content. The detector fingerprints the full returned result before
conversation truncation. Oversized shell output uses a fingerprint of the complete captured output.
Only recognized pytest summary durations are normalized; arbitrary timestamps, paths and numbers
are preserved. Stuck audit events contain tool name, pattern, count and action, never result/argument
fingerprints or raw contents. Existing permission audit fields are separate.

Actual file-tool content changes advance a task edit version shared with children. Failed or identical
writes do not advance it. Successful no-op writes and context compaction have separate repetition
counters and cannot erase the previous non-progress pattern. Reading different files or receiving
changed results breaks the corresponding consecutive-result sequence.

Both OpenAI-compatible and Anthropic loops collect observations in model tool-call order. Concurrent
OpenAI batches and speculative Anthropic streaming calls may already be running when a stop is
recognized; their outputs are drained and retained. No subsequent batch/model call is started. Tool
result placeholders preserve protocol pairing for calls skipped after stopping.

`run_once` returns `status` and `stop_reason` alongside text and tokens. Both child entry points return
JSON containing these fields and partial text, allowing the parent to continue rather than treating a
child stop as a task-wide exception. In sandbox CLI mode, a task is reported as completed only when
`run_once` reports completion and the container is still available. When `run_once` returns normally
after a stop (repetition detection or a budget/turn limit) and the container is still available, the
existing patch is exported. On timeout, other exceptions, or after the container has been destroyed,
only output already received is saved; no patch can be exported. A lost container is reported as
`stop_reason: Sandbox unavailable`, including when a child agent caused it. In every case
`result.json` keeps previous tool/test output and a non-completed status, and the container is cleaned
up. The CLI exits unsuccessfully to distinguish a stopped task from completion; tests are not rerun at
shutdown.

Accepted limitations:

- No A/B alternating-cycle detection. Unrelated real edits can advance the global edit version and
  interrupt a result pattern; this is not a semantic measure of useful progress.
- No complete tracking of filesystem changes caused by shell commands. Shell calls rely on returned
  output and exit status; identical-output polling or silent filesystem changes can be misclassified.
  Legitimate unchanged polling may stop on the fourth observation; raise thresholds when appropriate.
- Error-prefix adaptation remains for legacy local handlers, and conservative normalization may miss
  repetition involving changing timestamps or temporary paths. The detector does not establish that
  every repeated operation is useless or that every non-repeated operation makes progress.
