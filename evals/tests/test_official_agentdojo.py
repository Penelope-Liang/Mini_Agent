import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from agentdojo.agent_pipeline.ground_truth_pipeline import GroundTruthPipeline
from agentdojo.attacks import load_attack
from agentdojo.benchmark import benchmark_suite_with_injections, load_task_results
from agentdojo.logging import OutputLogger
from agentdojo.task_suite.load_suites import get_suite

from evals.agentdojo import MiniAgentPipeline, VERSION
from evals.official_agentdojo import OfficialPipeline


class OfficialRunnerTests(unittest.TestCase):
    def test_official_scoring_traces_capability_checks_and_resume(self):
        suite = get_suite(VERSION, 'banking')
        calls = []

        def scripted(pipeline, query, runtime, env, messages=(), extra_args=None):
            calls.append(query)
            task = next((task for task in suite.injection_tasks.values() if task.GOAL == query),
                        suite.get_user_task_by_id('user_task_0'))
            result = GroundTruthPipeline(task).query(query, runtime, env)
            pipeline.metrics.append({'error': None, 'approver_errors': 0})
            return result

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            pipeline = OfficialPipeline('default', root / 'adapter', api_key='offline-test-key',
                                        approver='provenance', policy='mode-only')
            attack = load_attack('direct', suite, pipeline)
            with patch.object(MiniAgentPipeline, 'query', scripted), OutputLogger(str(root)):
                result = benchmark_suite_with_injections(
                    pipeline, suite, attack, root, False,
                    user_tasks=['user_task_0'], injection_tasks=['injection_task_0'],
                    benchmark_version=VERSION)
                self.assertEqual(len(calls), 2)  # Goal-as-user capability + attacked task.
                self.assertTrue(result['injection_tasks_utility_results']['injection_task_0'])
                self.assertTrue(result['utility_results'][('user_task_0', 'injection_task_0')])
                trace = load_task_results(pipeline.name, 'banking', 'user_task_0',
                                          'direct', 'injection_task_0', root)
                self.assertGreaterEqual(len(trace.messages), 2)
                benchmark_suite_with_injections(
                    pipeline, suite, attack, root, False,
                    user_tasks=['user_task_0'], injection_tasks=['injection_task_0'],
                    benchmark_version=VERSION)
                self.assertEqual(len(calls), 2)
                dos = load_attack('dos', suite, pipeline)
                result = benchmark_suite_with_injections(
                    pipeline, suite, dos, root, False, user_tasks=['user_task_0'],
                    benchmark_version=VERSION)
                self.assertEqual(len(calls), 3)
                self.assertEqual(len(result['security_results']), 1)
                self.assertFalse(next(iter(result['security_results'].values())))
                self.assertFalse(result['injection_tasks_utility_results'])


if __name__ == '__main__':
    unittest.main()
