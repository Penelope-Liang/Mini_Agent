"""Evaluate MiniAgent using AgentDojo's official benchmark functions and traces.

The CLI defaults to all suites and all registered non-interactive attacks.
Processes isolate MiniAgent's per-query permission cache. Official task caches
are reused only within the same recorded source/configuration snapshot.
"""
import argparse
from concurrent.futures import ProcessPoolExecutor, as_completed
import hashlib
import json
import os
from pathlib import Path
import subprocess
import time
import uuid

from dotenv import load_dotenv
from agentdojo.attacks import load_attack
from agentdojo.attacks.attack_registry import ATTACKS
from agentdojo.benchmark import (
    benchmark_suite_with_injections, benchmark_suite_without_injections,
    run_task_without_injection_tasks,
)
from agentdojo.logging import Logger, OutputLogger
from agentdojo.task_suite.load_suites import get_suite, get_suites

from evals.agentdojo import MiniAgentPipeline, VERSION, encode


class OfficialPipeline(MiniAgentPipeline):
    def query(self, query, runtime, env, messages=(), extra_args=None):
        result = super().query(query, runtime, env, messages, extra_args)
        metric = self.metrics[-1]
        logger = Logger.get()
        logger.log(result[3])
        error = metric.get('error')
        if error:
            logger.log_error(error)
        # Do not turn an unavailable API or broken adapter into a successful defense.
        # Bounded-agent exhaustion is a measured agent failure, not an API outage.
        bounded = error and (error.startswith('RuntimeError: Eight-request limit') or
                             error.startswith('RuntimeError: Input size limit') or
                             error == 'Agent stopped before a final answer')
        if (error and not bounded) or metric.get('approver_errors'):
            raise RuntimeError(error or 'Approval API failed')
        return result


def write_json(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix('.tmp')
    temporary.write_text(json.dumps(value, indent=2))
    temporary.replace(path)


def serialize_results(results):
    return {
        key: [{'task': pair[0], 'injection': pair[1], 'passed': value}
              for pair, value in values.items()]
        if key != 'injection_tasks_utility_results' else values
        for key, values in results.items()
    }


def run_job(job):
    root = Path(job['root'])
    load_dotenv('.env')
    suite = get_suite(VERSION, job['suite'])
    output = root / 'adapter' / job['id'] / uuid.uuid4().hex
    pipeline = OfficialPipeline(
        'default', output, model=job['model'],
        api_key=os.getenv('OPENAI_API_KEY') or os.getenv('APIKEY'),
        base_url=os.getenv('OPENAI_BASE_URL') or 'https://api.openai.com/v1',
        approver='provenance', policy='mode-only', suite=job['suite'],
    )
    pipeline.name = f"{job['model']}-miniagent-default-provenance"
    record = {'job': job, 'status': 'running'}
    # Tool output goes to a per-job file, not the controller terminal.
    import contextlib
    output.mkdir(parents=True, exist_ok=True)
    try:
        with (output / 'console.log').open('w') as log, contextlib.redirect_stdout(log), contextlib.redirect_stderr(log):
            with OutputLogger(str(root / 'official')):
                if job['attack'] == 'none':
                    results = benchmark_suite_without_injections(
                        pipeline, suite, root / 'official', False, benchmark_version=VERSION)
                    # The official attack runner performs these checks too, and reuses
                    # this cache. Completing them first avoids duplicate concurrent writes.
                    capabilities = {}
                    for task in suite.injection_tasks.values():
                        capabilities[task.ID], _ = run_task_without_injection_tasks(
                            suite, pipeline, task, root / 'official', False, VERSION)
                    results['injection_tasks_utility_results'] = capabilities
                else:
                    attack = load_attack(job['attack'], suite, pipeline)
                    results = benchmark_suite_with_injections(
                        pipeline, suite, attack, root / 'official', False,
                        user_tasks=job['tasks'], benchmark_version=VERSION)
        record.update(status='completed', results=serialize_results(results))
    except Exception as exc:
        record.update(status='error', error=f'{type(exc).__name__}: {exc}')
    record['queries_executed'] = len(pipeline.metrics)
    record['metrics'] = pipeline.metrics
    write_json(root / 'jobs' / f"{job['id']}.json", record)
    return record


def source_digest():
    digest = hashlib.sha256()
    for path in sorted([*Path('agents').glob('*.py'), *Path('evals').glob('*.py')]):
        digest.update(str(path).encode())
        digest.update(path.read_bytes())
    return digest.hexdigest()


def summarize(root, manifest):
    groups = {}
    errors = []
    completed = 0
    for path in sorted((root / 'jobs').glob('*.json')):
        record = json.loads(path.read_text())
        if record['status'] != 'completed':
            errors.append({'job': record['job']['id'], 'error': record['error']})
            continue
        completed += 1
        job = record['job']
        group = groups.setdefault(job['suite'], {}).setdefault(job['attack'], {
            'tasks': 0, 'utility_successes': 0, 'attack_successes': 0,
            'agent_errors': 0, 'injection_goal_capability': {},
        })
        results = record['results']
        group['tasks'] += len(results['utility_results'])
        group['utility_successes'] += sum(r['passed'] for r in results['utility_results'])
        if job['attack'] != 'none':
            group['attack_successes'] += sum(r['passed'] for r in results['security_results'])
        group['agent_errors'] += sum(bool(m['error']) for m in record['metrics'])
        group['injection_goal_capability'].update(results['injection_tasks_utility_results'])
    summary = {'status': 'completed' if completed == manifest['expected_jobs'] and not errors else 'incomplete',
               'completed_jobs': completed, 'expected_jobs': manifest['expected_jobs'],
               'expected_queries': manifest['expected_queries'], 'errors': errors, 'suites': groups}
    write_json(root / 'summary.json', summary)
    return summary


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--run', action='store_true')
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--workers', type=int, default=8)
    parser.add_argument('--model', default='gpt-4o')
    parser.add_argument('--suites', default='all')
    parser.add_argument('--attacks', default='all')
    args = parser.parse_args()
    if args.workers < 1:
        parser.error('--workers must be positive')
    suites = get_suites(VERSION)
    names = list(suites) if args.suites == 'all' else args.suites.split(',')
    attacks = [name for name in ATTACKS if name != 'manual'] if args.attacks == 'all' else args.attacks.split(',')
    if any(name not in suites for name in names) or any(name not in ATTACKS or name == 'manual' for name in attacks):
        parser.error('Unknown suite/attack, or interactive manual attack requested')
    root = args.output.resolve()
    clean_jobs, attack_jobs = [], []
    queries = 0
    for name in names:
        suite = suites[name]
        base = {'root': str(root), 'suite': name, 'model': args.model}
        clean_jobs.append({**base, 'id': f'{name}-none', 'attack': 'none'})
        queries += len(suite.user_tasks) + len(suite.injection_tasks)
        tasks = list(suite.user_tasks)
        for attack in attacks:
            queries += len(tasks) * (1 if ATTACKS[attack].is_dos_attack else len(suite.injection_tasks))
            for start in range(0, len(tasks), 5):
                attack_jobs.append({**base, 'id': f'{name}-{attack}-{start}',
                                    'attack': attack, 'tasks': tasks[start:start + 5]})
    manifest = {
        'benchmark': 'AgentDojo', 'version': VERSION, 'model': args.model,
        'config': 'default:provenance:mode-only', 'source_digest': source_digest(),
        'suites': names, 'attacks': attacks, 'excluded_attacks': {'manual': 'Requires human-authored input'},
        'expected_queries': queries, 'expected_jobs': len(clean_jobs) + len(attack_jobs),
        'agent_limits': {'requests': 8, 'max_output_tokens_per_request': 1024, 'max_turns': 9,
                         'estimated_cost_check_usd': 0.15, 'temperature': 0},
        'scoring': 'Official benchmark_suite_without_injections / benchmark_suite_with_injections',
        'dos': 'Official single injection target per task; attack success = not utility',
    }
    print(encode(manifest), flush=True)
    if not args.run:
        return
    load_dotenv('.env')
    if not (os.getenv('OPENAI_API_KEY') or os.getenv('APIKEY')):
        parser.error('No model API credential configured')
    root.mkdir(parents=True, exist_ok=True)
    manifest_path = root / 'manifest.json'
    if manifest_path.exists() and json.loads(manifest_path.read_text()) != manifest:
        parser.error('Output contains a different source/configuration; use a new directory')
    write_json(manifest_path, manifest)
    (root / 'source.diff').write_text(subprocess.run(['git', 'diff', '--', 'agents', 'evals'], capture_output=True, text=True, check=True).stdout)
    # Include new files as well as tracked modifications for reproducibility.
    for path in [*Path('agents').glob('*.py'), *Path('evals').glob('*.py')]:
        copy = root / 'source' / path
        copy.parent.mkdir(parents=True, exist_ok=True)
        copy.write_bytes(path.read_bytes())
    write_json(root / 'progress.json', {'status': 'running', 'pid': os.getpid(), 'started': time.time()})
    failed = False
    for phase in (clean_jobs, attack_jobs):
        pending = [job for job in phase if not (root / 'jobs' / f"{job['id']}.json").exists()
                   or json.loads((root / 'jobs' / f"{job['id']}.json").read_text())['status'] != 'completed']
        with ProcessPoolExecutor(max_workers=args.workers) as executor:
            futures = {executor.submit(run_job, job): job for job in pending}
            for future in as_completed(futures):
                if future.cancelled():
                    continue
                record = future.result()
                print(json.dumps({'job': record['job']['id'], 'status': record['status'],
                                  'error': record.get('error')}), flush=True)
                summarize(root, manifest)
                if record['status'] == 'error':
                    failed = True
                    for other in futures:
                        other.cancel()
        if failed:
            break
    summary = summarize(root, manifest)
    write_json(root / 'progress.json', {'status': summary['status'], 'finished': time.time()})
    print(f"{summary['status']}: {root / 'summary.json'}", flush=True)
    if summary['status'] != 'completed':
        raise SystemExit(1)


if __name__ == '__main__':
    main()
