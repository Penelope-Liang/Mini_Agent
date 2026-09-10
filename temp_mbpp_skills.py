"""Temporary paired MBPP experiment; uses temp_mbpp.py without core edits.

Same tasks, prompt, model, tools, isolation, and five-response limit in both arms.
Only the presence of the English Skill during task execution differs.
Memory, MCP, subagents and automatic evolution remain disabled in both arms.
Runs native retrieval/skill invocation and online_skill_eval's deterministic
checks; no LLM judge calls, automatic promotion, or host skill modifications.
"""
import json
from pathlib import Path
import sys
import tempfile

import temp_mbpp as base

SKILL = '''---
name: python_function_workflow
description: Implement Python functions by matching the example signature, writing solution.py, running a check, and fixing failures.
user-invocable: false
when-to-use: Use when asked to write a Python function or implement a small programming task in solution.py and run a local check.
---

# Python Function Workflow

1. Match the exact function name, argument count, and return type in the task's example.
2. Implement the requested behavior in /workspace/solution.py. Derive a general solution; do not hardcode example outputs.
3. Use run_shell to run the supplied example against the actual file.
4. If a check fails, inspect the error, edit the implementation, and rerun the check within the available budget.
5. End with a short summary of the implementation and the check actually run, in at most 2 paragraphs. Do not claim to have run unavailable tests.
'''

SETUP = '''
SKILL_TEXT = {skill!r}
SKILL_ENABLED = {enabled!r}
skill_path = Path('/workspace/.miniagent/skills/python_function_workflow/SKILL.md')
def install_experiment_skill():
    skill_path.parent.mkdir(parents=True, exist_ok=True)
    skill_path.write_text(SKILL_TEXT)
if SKILL_ENABLED:
    install_experiment_skill()
'''

EVALUATION = r'''
# Score the observed reply against the same Skill in both arms. In the off arm,
# install it only after the task ended, so it cannot influence model behavior.
from agents.skills import reset_skill_cache
from agents.skill_evolution import get_evolution_dir, ONLINE_PROVENANCE_INDEX
from agents.online_skill_eval import evaluate_online_skill_evolution
install_experiment_skill()
reset_skill_cache()
answer = ''.join(agent._output_buffer or [])
source = {'messages': [
    {'role': 'user', 'content': Path('/workspace/prompt.txt').read_text()},
    {'role': 'assistant', 'content': answer},
], 'action': 'evaluation_replay', 'ok': status == 'finished'}
evolution_dir = get_evolution_dir()
evolution_dir.mkdir(parents=True, exist_ok=True)
(evolution_dir / ONLINE_PROVENANCE_INDEX).write_text(json.dumps({
    'python_function_workflow': {'sources': [source]}
}))
native = evaluate_online_skill_evolution(write_report=False, write_artifacts=False)
Path('/logs/online_skill_eval.json').write_text(json.dumps(native, indent=2))
Path('/logs/skill_evidence.json').write_text(json.dumps({
    'enabled_during_task': SKILL_ENABLED,
    'retrieval_hits': agent._last_retrieved_skill_hits,
    'skill_invocations': [c for c in calls if c['name'] == 'skill'],
    'skill_file_reads': [c for c in calls if c['name'] == 'read_file' and
                        'SKILL.md' in str(c['input'])],
    'native_eval_note': 'Deterministic response rules only; no LLM judge or workflow correctness claim.',
}, indent=2))
'''


def runner(enabled):
    code = base.RUNNER.replace(
        'from agents.agent import Agent',
        SETUP.format(skill=SKILL, enabled=enabled) + '\nfrom agents.agent import Agent',
    ).replace('is_sub_agent=True,', 'is_sub_agent=False,').replace(
        "'list_files', 'grep_search'}", "'list_files', 'grep_search', 'skill'}"
    ).replace('calls = []', '''
# Keep background model requests and external tools out of both experiment arms.
agent._mcp_initialized = True
agent._build_side_query = lambda **kwargs: None
calls = []''')
    return code + '\n' + EVALUATION


def main():
    original = base.RUNNER
    for enabled in (False, True):
        compile(runner(enabled), 'skills_runner.py', 'exec')
    if sys.argv[1:] == ['--check']:
        print('Both paired runners compile; English Skill is ASCII:', SKILL.isascii())
        return
    if sys.argv[1:]:
        raise SystemExit('Usage: temp_mbpp_skills.py [--check]')
    comparison = Path(tempfile.mkdtemp(prefix='skills-comparison-', dir=base.DATA.parent))
    (comparison / 'SKILL.md').write_text(SKILL)
    combined = {'protocol': __doc__, 'arms': {}}
    try:
        for label, enabled in [('off', False), ('on', True)]:
            base.RUNNER = original
            base.RUNNER = runner(enabled)
            base.IMAGE = 'miniagent-mbpp-skills-temp:' + label
            print('Running Skills ' + label.upper(), flush=True)
            directory = base.run()
            report = json.loads((directory / 'report.json').read_text())
            for row in report['results']:
                logs = directory / str(row['task_id'])
                row['skill_evidence'] = json.loads((logs / 'skill_evidence.json').read_text())
                row['online_skill_eval'] = json.loads((logs / 'online_skill_eval.json').read_text())
            combined['arms'][label] = {'directory': str(directory), **report}
            (comparison / 'comparison.json').write_text(json.dumps(combined, indent=2))
    finally:
        base.RUNNER = original
    for label, arm in combined['arms'].items():
        rows = arm['results']
        print(label, {'passed': sum(r['passed'] for r in rows),
                      'rounds': sum(r['model_rounds'] for r in rows),
                      'input_tokens': sum(r['input_tokens'] for r in rows),
                      'output_tokens': sum(r['output_tokens'] for r in rows),
                      'retrieved_tasks': sum(bool(r['skill_evidence']['retrieval_hits']) for r in rows),
                      'invoked_tasks': sum(bool(r['skill_evidence']['skill_invocations']) for r in rows)}, flush=True)
    print('Comparison:', comparison / 'comparison.json', flush=True)


if __name__ == '__main__':
    main()
