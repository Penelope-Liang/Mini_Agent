# Skill Self-Evolution

How Skills in MiniAgent are discovered, invoked, written, evolved, and governed.

This is not a black-box "automatic learning" system. It is an auditable online persistence pipeline:

```text
current task -> retrieve Skill -> execute
next-turn feedback -> extract candidate -> decide add / merge / discard
write SKILL.md -> record provenance -> track usage
```

## Design goals

Skills persist *methods*, not *facts*. Memory is for facts, preferences, and project background; Skills are for reusable workflows, output conventions, judgment criteria, and trigger conditions.

The problems this pipeline addresses:

- how to durably keep stable rules a user repeats;
- how to stop one-off task content from becoming long-term behaviour;
- how to prevent Skills from growing endlessly and redundantly;
- how to trace the source, version, and outcome of every write;
- whether a Skill is actually being used.

Operating principles:

- persist only stable, reusable, future-applicable rules;
- never persist one-off payloads, secrets, credentials, URLs, exact dates, or temporary project facts;
- prefer merging into an existing Skill over creating a new one;
- keep provenance and version snapshots for every write.

## The pipeline

Four segments:

1. **Online retrieval** — inject candidate Skills into the current conversation.
2. **Online persistence** — extract candidates from the conversation plus next-turn feedback.
3. **Maintenance decision** — decide add / merge / discard.
4. **Governance feedback** — record provenance, versions, usage stats, and archival.

They are separated because Skill systems usually fail in one of two ways: turning one-off content into permanent rules, or accumulating many near-duplicate Skills. The pending window, Extractor, Maintainer, provenance, and usage stats each guard against one of those.

## 1. Retrieval in the current request

After user input reaches `agents/agent.py`, relevant Skills are retrieved first:

```text
agents/agent.py::_augment_user_message_with_skill_context()
  -> agents/skills.py::format_retrieved_skill_context()
  -> agents/skills.py::retrieve_relevant_skills()
```

Retrieval is lightweight BM25:

- the query is the current user message;
- the Skill document is built from `name`, `description`, `when-to-use`, and the first 2,500 characters of the body;
- metadata is weighted higher, but body text still participates;
- basic tokenization covers both English and Chinese input;
- at most three candidates are returned by default.

Injected form:

```text
<retrieved_skills>
1. skill_name (score=..., source=project): description
   When to use: ...
</retrieved_skills>
```

This is a hint, not a forced invocation. The model still decides whether to call the `skill` tool, based on `when-to-use`.

## 2. Invocation

A Skill can be triggered two ways: the model calls the `skill` tool, or the user types `/<skill-name> [args]` in the REPL.

```text
skill tool call
  -> get_skill_by_name()
  -> record_skill_invocation()
  -> resolve_skill_prompt()
  -> inline or fork execution
```

`inline` injects the Skill's instructions back into the current conversation. `fork` runs it in a sub-agent; if the Skill declares `allowed-tools`, the sub-agent receives only those tools.

Every invocation is appended to `.miniagent/skill-evolution/usage.jsonl`.

## 3. The pending window

Online persistence does not write a Skill the moment a turn ends. It saves a *pending window* and waits for the next round of user feedback.

- `agents/agent.py::_set_pending_skill_extraction_window()`
- `agents/agent.py::_pop_pending_skill_extraction_window()`

The window holds up to eight recent messages, the original user input, the assistant output, the top retrieved Skill reference for that turn, and the session id.

When the next user message arrives, the window is merged with that feedback into `ready_skill_extraction_window` and enters the evolution pipeline.

The reasoning: the user's next turn usually contains a correction or a stated preference, which is far more reliable evidence than the assistant guessing what it just learned.

## 4. `online_ingest()`

The unified entry point is `agents/online_skill_evolution.py::online_ingest()`:

```text
messages + retrieved_reference + hint
  -> extract_online_skill_candidate()
  -> no candidate: action = none
  -> candidate: maintain_online_skill_candidate()
  -> record_online_provenance()
```

| Action | Meaning |
|--------|---------|
| `none` | Nothing worth persisting was found |
| `discard` | A candidate existed but was duplicate, low value, or unsuitable |
| `add` | Create a new Skill |
| `merge` | Merge into an existing Skill |
| `failed` | Pipeline error |
| `add_denied` / `merge_denied` | Write blocked by permissions |

## 5. Extractor — propose only

`extract_online_skill_candidate()` takes the conversation window, an optional hint, and the retrieved reference, and returns a candidate as strict JSON:

```json
{
  "skills": [
    {
      "name": "...",
      "description": "...",
      "when_to_use": "...",
      "instructions": "...",
      "evidence": "...",
      "tags": []
    }
  ]
}
```

Hard constraints:

- user messages are the primary evidence; assistant messages are context only;
- never extract one-off content, private data, secrets, URLs, credentials, exact dates, or temporary parameters;
- extract only future-valuable workflows, output strategies, stable corrections, or repeated constraints;
- return empty when the evidence is weak.

The Extractor never writes files. It only proposes.

## 6. Maintainer — add, merge, or discard

`maintain_online_skill_candidate()` decides what happens to a candidate:

```text
candidate
  -> discover_skills()
  -> exact identity match
  -> retrieve_relevant_skills(limit=8, min_score=0.03)
  -> LLM decides add / merge / discard
  -> rule-based fallback correction
```

The rule-based fallback exists because the model can be over-eager:

- if name, description, and when-to-use fully overlap an existing Skill, force `merge`;
- if the model says `add` but the closest existing Skill scores ≥ 0.55, change it to `merge`;
- if `merge` has no target, fall back to the retrieved reference;
- an invalid action downgrades to `discard`.

| Result | Next action |
|--------|-------------|
| `add` | `create_skill_file()` writes a new `SKILL.md` |
| `merge` | `evolve_skill_file()` evolves an existing `SKILL.md` |
| `discard` | No file write; provenance is still recorded |

## 7. Writes converge in `skill_evolution.py`

Whether a write comes from the online pipeline or from a manual `/skill-create` / `/skill-evolve`, it goes through `agents/skill_evolution.py`.

### Creating

```text
create_skill()
  -> create_skill_file()
  -> check for a same-name Skill
  -> generate a safe directory name
  -> write .miniagent/skills/<slug>/SKILL.md
  -> usage.jsonl records the create
```

Default frontmatter fields: `name`, `description`, `version`, `created-at`, `user-invocable`, `context`, `when-to-use`, `tags`, `allowed-tools`.

### Evolving

```text
evolve_skill()
  -> evolve_skill_file()
  -> locate the existing SKILL.md
  -> snapshot the full pre-evolution content
  -> bump the patch version
  -> update last-evolved / evolution-count
  -> write back the merged body
  -> usage.jsonl records the evolve
```

Snapshots go to `.miniagent/skill-evolution/history/<skill_slug>.jsonl`. Versions only bump the patch component: `0.1.0 -> 0.1.1 -> 0.1.2`.

## 8. Provenance

- `agents/skill_evolution.py::record_online_skill_provenance()`
- `agents/skill_evolution.py::_update_online_provenance_index()`

Each online persistence appends to `.miniagent/skill-evolution/online_provenance.jsonl` and is aggregated per Skill into `.miniagent/skill-evolution/online_skill_provenance.json`.

Recorded fields include the action, the Skill, the messages, the retrieved reference, the decision, the result, and any error. The point is that you can answer not only *what* was written, but *why*, *from what evidence*, and *what happened*.

## 9. Usage statistics

Adding Skills is not the goal; Skills being useful is. After each reply, if Skills were retrieved this turn, the runtime judges whether the Skill was relevant to the request and whether the reply actually followed its method.

- `agents/agent.py::_run_skill_usage_tracking()`
- `agents/online_skill_evolution.py::judge_retrieved_skill_usage()`
- `agents/skill_evolution.py::record_skill_usage_judgments()`

Statistics are written to `.miniagent/skill-evolution/skill_usage_stats.json`:

| Field | Meaning |
|-------|---------|
| `retrieved` | Times retrieved |
| `relevant` | Times judged relevant |
| `used` | Times judged actually used |
| `last_retrieved` | Last retrieval time |
| `last_used` | Last actual use |
| `last_reason` | Reason behind the last judgment |

A Skill that is retrieved repeatedly but never used is treated as stale and, once thresholds and permissions allow, archived to `.miniagent/skill-evolution/pruned/`.

Thresholds can be tuned with `MINIAGENT_SKILL_USAGE_PRUNE_MIN_RETRIEVED`, `MINIAGENT_SKILL_USAGE_PRUNE_MAX_USED`, and `MINIAGENT_SKILL_PRUNE_PROJECT`.

## 10. Switches and permissions

```bash
MINIAGENT_AUTO_SKILL_EVOLUTION=1    # enable online evolution (default on)
MINIAGENT_AUTO_SKILL_TARGET=project # or: user
```

`project` writes to `<project>/.miniagent/skills/`; `user` writes to `~/.miniagent/skills/`, making the Skill available everywhere.

Permission mode changes what evolution can do:

| Mode | Behaviour |
|------|-----------|
| `plan` | Background evolution and usage tracking are not scheduled at all |
| `default` | Writes generally require confirmation |
| `acceptEdits` | Edit-class operations are auto-approved, so background writes work |
| `bypassPermissions` | No confirmation |
| `dontAsk` | Writes that would prompt are auto-denied |

`skill_create` and `skill_evolve` are classified as edit-class tools in `tools.py`.

## 11. Manual entry points

REPL:

```text
/skills
/skill-stats
/extract_now [hint]
/skill-feedback <skill> <rating> [note]
/skill-evolve <skill> <durable lesson>
/skill-create <name> | <description> | <when-to-use> | <instructions>
/<skill-name> [args]
```

Tools: `skill`, `skill_create`, `skill_evolve`.

When to use which:

- `/extract_now` — probe whether the current window is worth persisting;
- `/skill-create` — the workflow is already stable;
- `/skill-evolve` — an existing Skill needs one more rule;
- `/skill-feedback` — record an opinion without changing the Skill.

## 12. What good feedback looks like

Self-evolution is not meant to turn every sentence into a Skill.

Not worth persisting — a one-off request:

```text
Review this file for bugs.
```

Worth persisting — a durable rule:

```text
When reviewing code from now on, always list findings by severity with file and line references, and call out missing tests explicitly rather than summarizing.
```

Worth merging into an existing Skill:

```text
For code reviews going forward, also flag async cancellation and resource-leak risks.
```

## 13. Four closed loops

**Retrieval**

```text
discover_skills()
  -> retrieve_relevant_skills()
  -> retrieved_skills injection
  -> model decides based on when-to-use
```

**Persistence**

```text
task and answer
  -> pending window
  -> next-turn user feedback
  -> Extractor proposes a candidate
  -> Maintainer decides add / merge / discard
  -> SKILL.md written
```

**Audit**

```text
create / evolve / discard / failed
  -> usage.jsonl
  -> online_provenance.jsonl
  -> online_skill_provenance.json
  -> history snapshot
```

**Quality feedback**

```text
retrieved Skills
  -> judge relevance and use
  -> skill_usage_stats.json
  -> stale pruning
```

## 14. Where the code lives

| Logic | Entry point |
|-------|-------------|
| Skill loading and retrieval | `agents/skills.py` |
| Pending window | `agents/agent.py` |
| Online extraction and maintenance | `agents/online_skill_evolution.py` |
| Create / evolve / provenance / usage | `agents/skill_evolution.py` |
| Tool entry and permissions | `agents/tools.py` |

## Observable state

This repository ships one general-purpose Skill with English instructions:

```text
.miniagent/skills/code_review/SKILL.md
```

Audit artifacts under `.miniagent/skill-evolution/` are generated as you use MiniAgent and are excluded from version control:

```text
usage.jsonl
online_provenance.jsonl
online_skill_provenance.json
skill_usage_stats.json
history/
pruned/
```

## In one sentence

MiniAgent's Skill self-evolution does not let the model rewrite itself freely; it turns user-confirmed stable workflows into reusable, traceable, governable `SKILL.md` files through extraction, decision, write, audit, and measurement.
