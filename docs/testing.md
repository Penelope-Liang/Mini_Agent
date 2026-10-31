# Testing and evaluation

[Back to README](../README.md)

Run commands from the repository root. AgentDojo evaluates tool-use utility and prompt-injection behavior; it does not measure coding-task performance. Model-based evaluations incur API costs.

## Skill quality

Replay historical conversations against rules compiled from each `SKILL.md`, optionally with an LLM judge:

```bash
python3 -m agents.online_skill_eval
```

The retained library REPL also supports `/skill-eval`; this command is not available in the sandbox CLI. See [Skills evaluation](skills-evaluation.md).

## AgentDojo (tool calling security and utility)

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

Scored JSON dumps under `evals/results/` and `artifacts/` stay local (gitignored). Narrative and limits: [tests/docs/agentdojo-evaluation.md](../tests/docs/agentdojo-evaluation.md), [tests/docs/permissions-verification.md](../tests/docs/permissions-verification.md).

## Automated tests and CI

```bash
PYTHONPATH=. python -m unittest discover -s tests -v
PYTHONPATH=. python -m unittest discover -s evals/tests -v
```

Real container tests (no LLM calls, temporary fixtures and fake credentials only):

```bash
MINIAGENT_DOCKER_TESTS=1 python -m unittest tests.test_workspace -v
```

GitHub Actions runs these container tests and the unit/adapter suites on push and pull request (`.github/workflows/ci.yml`). Offline permission demo: `python -m examples.permissions_demo`.
