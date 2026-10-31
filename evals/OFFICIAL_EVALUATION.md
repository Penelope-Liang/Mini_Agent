# Official AgentDojo evaluation of MiniAgent

This runner uses the pinned AgentDojo package's `benchmark_suite_without_injections`
and `benchmark_suite_with_injections`, with its unmodified task definitions, attack
templates, capability checks, scoring, trace format and task cache. The pipeline
under evaluation is MiniAgent, not AgentDojo's built-in LLM pipeline.

```bash
# Inspect the scope without making API calls.
.venv-agentdojo/bin/python -m evals.official_agentdojo --output artifacts/agentdojo-official-20261030

# Run, or resume the identical source/configuration in the same directory.
.venv-agentdojo/bin/python -u -m evals.official_agentdojo --run \
  --output artifacts/agentdojo-official-20261030 --workers 8
```

Defaults: task version `v1.2.2`, model `gpt-4o`, configuration
`default:provenance:mode-only`. All four suites and all 16 registered automatic
attacks are included. The interactive `manual` attack is excluded because it
requires a human to author each payload. This is an explicitly selected full
automatic-attack sweep, not a claim that the official CLI defaults to all attacks.
The official CLI takes one `--attack` per invocation.

There are 97 clean user tasks, 35 injection goals tested as explicit user requests,
10,439 goal-directed attacked task pairs, and 485 DoS task runs: 11,056 queries
before any resume or official retry behavior. DoS uses one injection target per
user task and scores attack success as failure of the legitimate task, exactly
as the official benchmark does. Keep DoS and goal-directed results separate.

Eight worker processes isolate the mutable permission-rule cache. Clean tasks and
injection-goal capability checks finish before attacked jobs begin. The official
cache avoids re-running those capability checks for every attack. An output
directory cannot be resumed with changed source/configuration. The manifest,
Python source snapshot and Git diff record the evaluated implementation.

Outputs:

- `official/`: official per-task traces and scores.
- `adapter/`: MiniAgent transcripts, permission audits and per-job console logs.
- `jobs/`: job results and execution/approver metrics.
- `summary.json`: per-suite, per-attack counts, capability checks and errors.
- `progress.json`: controller status; an incomplete run is not a final benchmark score.

The existing bounded adapter permits eight model requests, 1,024 output tokens
per request, nine agent turns, temperature zero, and the existing estimated
$0.15 agent-cost check. These are MiniAgent adapter settings, not official
AgentDojo defaults or a hard overall spending cap. Approval model usage is recorded
separately. The API model alias and configured endpoint are not guaranteed to be
identical to historical runs.

Model/approval API failures halt new job scheduling and leave the run incomplete;
they are not scored as successful defenses. Agent request/context exhaustion is
logged as a bounded-agent failure. Provenance checks are specialized for banking
and selected workspace tools; unmatched Slack/Travel actions fall back to the
isolated approver. The evaluation uses the benchmark's simulated tools and does
not measure Docker isolation or code-repair accuracy.

Official references at the pinned package revision:

- https://github.com/ethz-spylab/agentdojo/blob/089ed468cf3ed0322acc66b0211f26d9d90dbf60/src/agentdojo/scripts/benchmark.py
- https://github.com/ethz-spylab/agentdojo/blob/089ed468cf3ed0322acc66b0211f26d9d90dbf60/src/agentdojo/benchmark.py
