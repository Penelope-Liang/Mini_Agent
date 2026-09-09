# Skills Evaluation

MiniAgent can evaluate the quality of its own Skills. This document explains what is measured, how to run it, and how to read the results.

## What this measures — and what it does not

This pipeline evaluates **Skills**: whether a Skill has real usage evidence, whether past responses satisfied the requirements written in its own `SKILL.md`, and whether the Skill was actually retrieved and used afterwards.

It is **not** an end-to-end task benchmark. It does not measure task success rates and it is not comparable to benchmarks such as GAIA or HLE. This repository does not currently ship a harness for those; the datasets under `data/` are third-party material and are not wired into an automated runner.

It also never overwrites your active Skill files. It observes, judges, trials candidate rewrites, and records — nothing more.

Implementation: `agents/online_skill_eval.py`.

## The approach

The pipeline does not ask "is this Skill well written?" directly. It first checks whether there is enough real evidence, then judges historical responses against requirements extracted from the Skill itself.

```text
decide which Skills to observe
  -> find real conversation sources for them
  -> organize history into reviewable replay samples
  -> extract checkable requirements from SKILL.md
  -> check whether past responses satisfied those requirements
  -> generate candidate improved versions from failing rules
  -> regenerate responses with candidates and evaluate
  -> merge in retrieved / relevant / used statistics
  -> produce a status and a reason
```

Consequences of designing it this way:

- A Skill with no conversations and no usage can only be marked unobserved.
- A Skill with some signal but too few samples goes into an observation period rather than being called healthy.
- A Skill is only healthy once sample count, rule pass rate, and usage all clear their thresholds.
- A candidate rewrite that clearly beats the current version may be recorded as a *champion* artifact, but the active `SKILL.md` is still not rewritten automatically.

Vocabulary:

| Term | Meaning |
|------|---------|
| `replay` | Evaluation samples built from real historical conversations |
| `lineage` | The full observation thread for one Skill |
| `rule` | A checkable requirement compiled from the Skill's own text |
| `LLM judge` | Using a model to decide whether a response satisfies a rule |
| `champion` | The best healthy version in the local evaluation records |

## Running it

### In the REPL

```text
/skill-eval
```

This path has access to the agent's model client, so LLM judge rules are compiled and executed:

```text
agents/main.py
  -> agent._build_side_query(max_tokens=2400)
  -> format_online_skill_eval_async(side_query=side_query)
  -> evaluate_online_skill_evolution_async(side_query=side_query)
```

### As a module

```bash
python3 -m agents.online_skill_eval
```

There is no `side_query` here, so only programmatic rules run.

| Path | LLM judge | Use case |
|------|-----------|----------|
| REPL `/skill-eval` | Yes | Normal manual review |
| Module run | No | Quick local check, scripting |

### Without side effects

To get a report without writing any artifacts:

```python
from agents.online_skill_eval import evaluate_online_skill_evolution

report = evaluate_online_skill_evolution(write_report=False, write_artifacts=False)
```

## Pipeline stages

```text
/skill-eval or module run
  -> read online provenance / usage stats / active Skills
  -> group into lineages by Skill name
  -> build and freeze the replay pool
  -> compile programmatic and llm_binary rules from SKILL.md
  -> evaluate historical assistant replies against the rules
  -> build candidate variants from failing rules
  -> regenerate replay responses with candidates and evaluate them
  -> merge retrieved / relevant / used
  -> compute status
  -> decide champion promotion
  -> write report and run artifacts
```

**Replay pool.** Samples come from the audit trail left by online evolution — the conversation windows that produced each Skill. The pool is frozen to `datasets/<lineage_id>/replay_pool.jsonl` so later runs evaluate the same samples, and split deterministically into dev and test portions.

**Rules.** Requirements are compiled from the Skill's own text: structural constraints become programmatic rules, and looser requirements become `llm_binary` rules that need a judge. Compiled rules are saved as `eval_spec.json`.

**Usage gate.** Rule results say whether replies satisfied requirements; they say nothing about whether the Skill mattered. The usage gate reads statistics recorded by the main pipeline:

```text
relevance_rate       = relevant / retrieved
used_rate            = used / retrieved
used_when_relevant   = used / relevant
```

## Status

Status combines replay volume, rule results, and usage — not rule pass rate alone.

| Status | Meaning |
|--------|---------|
| `unobserved` | No replay samples and no usage signal |
| `incubating` | Some signal, but too few replay samples, promotion tests, or retrievals |
| `watch` | Enough data, but rules, relevance rate, or usage rate fall below threshold |
| `healthy` | Replay, rules, and usage gate all pass |
| `pruned` | The Skill was archived by stale pruning |

Default thresholds:

| Threshold | Default |
|-----------|---------|
| `DEFAULT_MIN_REPLAY_SAMPLES` | 2 |
| `DEFAULT_MIN_PROMOTION_TESTS` | 1 |
| `DEFAULT_MIN_RETRIEVED` | 5 |
| `DEFAULT_MIN_USED_RATE` | 0.2 |
| `DEFAULT_MIN_RELEVANCE_RATE` | 0.35 |
| `DEFAULT_MIN_RULE_PASS_RATE` | 0.8 |

Evaluated in order: `pruned`, `unobserved`, `incubating`, `watch`, `healthy`. Insufficient data always yields `incubating` rather than an accidental `healthy` from a couple of lucky samples.

All thresholds are keyword arguments to `evaluate_online_skill_evolution()`.

## Champion

A champion is the known-good version for a lineage in the local records. It is not a release or rollback mechanism.

Only a `healthy` candidate can be promoted, and only if it improves on the current champion:

```text
candidate.status == healthy
candidate.average_score >= champion.average_score + DEFAULT_MIN_SCORE_DELTA   # 0.01
candidate.hard_failures <= champion.hard_failures
```

The promoted `SKILL.md` is written as an artifact for comparison. Your active Skill file is untouched.

## Artifacts

```text
.miniagent/skill-evolution/online-eval/
  datasets/<lineage_id>/replay_pool.jsonl
  evals/<lineage_id>/eval_spec.json
  runs/<lineage_id>/<run_id>/outputs.jsonl
  runs/<lineage_id>/<run_id>/judgments.jsonl
  runs/<lineage_id>/<run_id>/summary.json
  champions.json
  champions/<lineage_id>/champion.json
  champions/<lineage_id>/SKILL.md
```

| File | Purpose |
|------|---------|
| `replay_pool.jsonl` | The frozen sample pool |
| `eval_spec.json` | The rules compiled this run |
| `outputs.jsonl` | The assistant reply evaluated for each sample |
| `judgments.jsonl` | One judgment per rule per sample |
| `summary.json` | Status, scores, promotion result, artifact paths |
| `champions.json` | Global champion index |

`run_id` has the form `YYYYMMDDTHHMMSSZ-<lineage_id_suffix>`.

These are generated locally as you use MiniAgent and are excluded from version control.

## Reading the terminal summary

The summary is a compressed view of `online_eval_report.json`.

> Demo data only. The block below is illustrative, not a real evaluation run, and the paths are placeholders.

```text
Online skill eval:
  data_dir=<project>/.miniagent/skill-evolution
  aggregate: ingests=5, ok_rate=100.0%, candidate_events=1, acceptance_rate=100.0%,
             replay_samples=1, rule_pass_rate=50.0%, llm=on, llm_rules=1,
             llm_judgments=1, llm_pass_rate=0.0%, candidates=1
  actions: none=4, add=1, merge=0, discard=0, failed=0, denied=0
  statuses: incubating=1
  skills:
    code_review: status=incubating, replay=1 (test=0), rules=2, llm_rules=1,
                 candidates=1, rule_pass=50.0%, hard_failures=0, retrieved=2,
                 used_rate=50.0% - only 1 replay sample(s)
  report_file=<project>/.miniagent/skill-evolution/online_eval_report.json
```

How to read it:

- `rules` is the **total** rule count, LLM rules included, so `rules=2, llm_rules=1` means one programmatic rule and one judge rule. Pass rates are computed over judgments, one per rule per sample — here 2 rules × 1 sample = 2 judgments, the programmatic one passing and the judge one failing, giving `rule_pass=50.0%` and `llm_pass_rate=0.0%`.
- `llm=off, llm_rules=0` — the run had no `side_query`, so only programmatic rules executed. Expected when running the module directly.
- `llm=on, llm_rules>0, llm_judgments=0` — the judge is enabled and rules compiled, but there were no historical samples to evaluate.
- `status=incubating` with a small `replay` count — the Skill simply has not been exercised enough yet. Use it on real tasks and re-run.

Skills are listed in the order `watch`, `incubating`, `unobserved`, `pruned`, `healthy`, so anything needing attention appears first. Within a status, Skills with more samples and retrievals come first.

## What is supported today

Supported: lineage aggregation, frozen replay pools, stable dev/test splits, programmatic rules, LLM binary judge rules, heuristic and LLM-generated candidate variants, candidate response generation and evaluation, the usage gate, the status gate, run artifacts, and local champion records.

The boundary, stated plainly:

```text
Evaluation can call an LLM to judge whether rules are satisfied,
and can generate replay responses for candidate variants,
but it never overwrites the active Skill file.
```
