"""Disposable MBPP smoke test. Run using Harbor's Python (has python-dotenv).

  /path/to/harbor/bin/python temp_mbpp.py --check
  /path/to/harbor/bin/python temp_mbpp.py

Requires Docker and /tmp/miniagent-mbpp/sanitized-mbpp.json downloaded from
https://raw.githubusercontent.com/google-research/google-research/master/mbpp/sanitized-mbpp.json
Uses first five sanitized test-split tasks by ID, one visible example, three
official assertions for scoring. This is a custom smoke test, not standard MBPP.
Each task has a fresh container, five model rounds, and a 180-second outer limit.
Only six basic file/shell tools are enabled; no Skills, memory, MCP or subagents.
No changes to MiniAgent core files. Results and build context stay under /tmp.
"""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import time
import uuid

from dotenv import dotenv_values

ROOT = Path(__file__).resolve().parent
DATA = Path('/tmp/miniagent-mbpp/sanitized-mbpp.json')
IMAGE = 'miniagent-mbpp-temp:local'
KEYS = ('APIKEY', 'API', 'MODEL', 'OPENAI_API_KEY', 'OPENAI_BASE_URL',
        'ANTHROPIC_API_KEY', 'ANTHROPIC_BASE_URL', 'MINI_CLAUDE_API_KEY', 'MINI_CLAUDE_API_BASE')

RUNNER = r'''
import asyncio
import json
import os
from pathlib import Path
import traceback

config_file = Path('/tmp/miniagent-config.json')
os.environ.update(json.loads(config_file.read_text()))
config_file.unlink()
os.environ['MINIAGENT_AUTO_SKILL_EVOLUTION'] = '0'
from agents.agent import Agent
from agents.main import _resolve_api_config
from agents.tools import tool_definitions
from agents import session
session.SESSION_DIR = Path('/logs/sessions')

class RoundLimit(Exception):
    pass

class SmokeAgent(Agent):
    rounds = 0
    def count_round(self):
        if self.rounds >= 5:
            raise RoundLimit('Reached five model rounds')
        self.rounds += 1
    async def _call_openai_stream(self):
        self.count_round()
        return await super()._call_openai_stream()
    async def _call_anthropic_stream(self, on_tool_block_complete=None):
        self.count_round()
        return await super()._call_anthropic_stream(on_tool_block_complete)

api, key, use_openai = _resolve_api_config(None)
agent = SmokeAgent(
    permission_mode='bypassPermissions', model=os.environ.get('MODEL', 'deepseek-chat'),
    api_key=key, api_base=(api or 'https://api.openai.com/v1') if use_openai else None,
    anthropic_base_url=api if not use_openai else None,
    is_sub_agent=True,
    custom_tools=[t for t in tool_definitions if t['name'] in
                  {'read_file', 'write_file', 'edit_file', 'run_shell', 'list_files', 'grep_search'}],
)
calls = []
agent._output_buffer = []
original_execute = agent._execute_tool_call
async def recorded_execute(name, inp):
    calls.append({'name': name, 'input': inp})
    return await original_execute(name, inp)
agent._execute_tool_call = recorded_execute
status = 'finished'
try:
    asyncio.run(asyncio.wait_for(agent.chat(Path('/workspace/prompt.txt').read_text()), timeout=150))
except RoundLimit:
    status = 'round_limit'
except asyncio.TimeoutError:
    status = 'timeout'
except Exception:
    status = 'error'
    traceback.print_exc()
finally:
    Path('/logs/usage.json').write_text(json.dumps({
        'status': status, 'model_rounds': agent.rounds, 'tool_calls': calls,
        'input_tokens': agent.total_input_tokens, 'output_tokens': agent.total_output_tokens,
        'solution_written': Path('/workspace/solution.py').is_file(),
    }, indent=2))
    Path('/logs/messages.json').write_text(json.dumps(
        agent._openai_messages if agent.use_openai else agent._anthropic_messages,
        indent=2, default=lambda value: value.model_dump()))
    Path('/logs/answer.txt').write_text(''.join(agent._output_buffer))
'''


def command(args, *, timeout=180, check=True, log=None):
    result = subprocess.run(args, text=True, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, timeout=timeout)
    if log:
        Path(log).write_text(result.stdout)
    if check and result.returncode:
        raise RuntimeError(f'{args[0]} failed ({result.returncode}); see {log or result.stdout[-2000:]}')
    return result


def docker(*args, **kwargs):
    return command(['docker', *args], **kwargs)


def score(solution: Path, task: dict, directory: Path) -> dict:
    directory.mkdir(parents=True, exist_ok=True)
    if not solution.is_file():
        return {'passed': False, 'reason': 'solution.py missing', 'tests_passed': 0}
    imports = '\n'.join(task.get('test_imports', []))
    script = ('import json\nfrom solution import *\n' + imports + '\nchecks = ' + repr(task['test_list']) + '\n'
              'results = []\nfor check in checks:\n'
              '    try:\n        exec(check)\n        results.append(True)\n'
              '    except Exception:\n        results.append(False)\n'
              'print(json.dumps({"passed": all(results), "tests_passed": sum(results), "tests": results}))\n')
    test_path = directory / 'grade.py'
    test_path.write_text(script)
    name = 'miniagent-mbpp-grade-' + uuid.uuid4().hex[:10]
    try:
        docker('create', '--name', name, '--network', 'none', '--memory', '256m', '--cpus', '1',
               '--pids-limit', '64', '-w', '/workspace', IMAGE, 'python', '/workspace/grade.py')
        docker('cp', str(solution), name + ':/workspace/solution.py')
        docker('cp', str(test_path), name + ':/workspace/grade.py')
        result = docker('start', '-a', name, timeout=15, check=False, log=directory / 'grader.log')
        try:
            return json.loads(result.stdout.strip().splitlines()[-1])
        except (ValueError, IndexError):
            return {'passed': False, 'reason': 'grader execution error', 'tests_passed': 0}
    except subprocess.TimeoutExpired:
        return {'passed': False, 'reason': 'grader timeout', 'tests_passed': 0}
    finally:
        docker('rm', '-f', name, check=False)


def run():
    tasks = sorted((t for t in json.loads(DATA.read_text()) if 11 <= t['task_id'] <= 510),
                   key=lambda t: t['task_id'])[:5]
    assert len(tasks) == 5
    compile(RUNNER, 'runner.py', 'exec')
    file_config = dotenv_values(ROOT / '.env')
    config = {k: os.environ.get(k, file_config.get(k)) for k in KEYS}
    config = {k: v for k, v in config.items() if v}
    if sys.argv[1:] == ['--check']:
        print('Syntax OK; selected task IDs:', [t['task_id'] for t in tasks])
        return
    if sys.argv[1:]:
        raise SystemExit('Usage: temp_mbpp.py [--check]')
    if not any(config.get(k) for k in ('APIKEY', 'OPENAI_API_KEY', 'ANTHROPIC_API_KEY', 'MINI_CLAUDE_API_KEY')):
        raise ValueError('No configured model credential')
    output = Path(tempfile.mkdtemp(prefix='run-', dir=DATA.parent))
    print('Results:', output, flush=True)
    build = output / 'build'
    (build / 'agents').mkdir(parents=True)
    for source in (ROOT / 'agents').glob('*.py'):
        shutil.copy2(source, build / 'agents' / source.name)
    shutil.copy2(ROOT / 'requirements.txt', build / 'requirements.txt')
    (build / 'runner.py').write_text(RUNNER)
    (build / 'Dockerfile').write_text(
        'FROM python:3.11-slim\n'
        'ENV PYTHONPATH=/opt/miniagent PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1\n'
        'COPY requirements.txt /opt/miniagent/requirements.txt\n'
        'RUN pip install --no-cache-dir -r /opt/miniagent/requirements.txt\n'
        'COPY agents /opt/miniagent/agents\nCOPY runner.py /opt/miniagent/runner.py\n'
        'RUN mkdir -p /workspace /logs\nWORKDIR /workspace\n')
    docker('build', '-t', IMAGE, str(build), timeout=300, log=output / 'build.log')
    print('Image ready; checking grader against reference solutions.', flush=True)
    for task in tasks:
        reference = output / 'grader-validation' / str(task['task_id'])
        reference.mkdir(parents=True)
        (reference / 'solution.py').write_text(task['code'])
        assert score(reference / 'solution.py', task, reference)['passed'], 'Reference grading failed'
    bad = output / 'grader-validation' / 'bad.py'
    bad.write_text('raise RuntimeError("intentional negative check")\n')
    assert not score(bad, tasks[0], bad.parent / 'negative')['passed']
    report = {'dataset': 'MBPP sanitized, custom smoke protocol',
              'dataset_sha256': hashlib.sha256(DATA.read_bytes()).hexdigest(),
              'model': config.get('MODEL', 'deepseek-chat'), 'max_model_rounds': 5,
              'visible_tests_per_task': 1, 'scored_tests_per_task': 3, 'results': []}
    for task in tasks:
        directory = output / str(task['task_id'])
        directory.mkdir()
        prompt = ('Complete this task by using tools to write /workspace/solution.py and run a local check. '
                  'This is an unattended execution task; do not just provide advice or code in your reply. '
                  'You have at most five model responses. Work directly and do not ask for confirmation.\n\n'
                  + task['prompt'] + '\n\nMatch the function name and arguments in this example:\n'
                  + task['test_list'][0] + '\nAdditional tests will run after you finish.\n')
        (directory / 'prompt.txt').write_text(prompt)
        name = 'miniagent-mbpp-' + uuid.uuid4().hex[:10]
        started = time.monotonic()
        print(f'Task {task["task_id"]}: running (five rounds max)', flush=True)
        try:
            docker('run', '-d', '--name', name, '--memory', '512m', '--cpus', '1', '--pids-limit', '128',
                   IMAGE, 'sleep', '600')
            docker('cp', str(directory / 'prompt.txt'), name + ':/workspace/prompt.txt')
            with tempfile.TemporaryDirectory(prefix='miniagent-mbpp-key-') as temporary:
                secret = Path(temporary) / 'config.json'
                secret.write_text(json.dumps(config))
                secret.chmod(0o600)
                docker('cp', str(secret), name + ':/tmp/miniagent-config.json')
            result = docker('exec', name, 'python', '/opt/miniagent/runner.py', check=False,
                            timeout=180, log=directory / 'console.log')
            docker('cp', name + ':/logs/.', str(directory), check=False)
            docker('cp', name + ':/workspace/solution.py', str(directory / 'solution.py'), check=False)
        except subprocess.TimeoutExpired:
            (directory / 'timeout.txt').write_text('Outer 180-second timeout reached')
        finally:
            docker('rm', '-f', name, check=False)
        usage = json.loads((directory / 'usage.json').read_text()) if (directory / 'usage.json').exists() else {'status': 'execution_error'}
        grading = score(directory / 'solution.py', task, directory / 'grading')
        row = {'task_id': task['task_id'], 'prompt': task['prompt'],
               'elapsed_seconds': round(time.monotonic() - started, 1), **usage, **grading}
        report['results'].append(row)
        (output / 'report.json').write_text(json.dumps(report, indent=2))
        print(f'Task {task["task_id"]}: passed={grading["passed"]}, rounds={usage.get("model_rounds")}, '
              f'tools={len(usage.get("tool_calls", []))}', flush=True)
    print(f'Passed {sum(r["passed"] for r in report["results"])}/5. Report: {output / "report.json"}', flush=True)
    return output


if __name__ == '__main__':
    run()
