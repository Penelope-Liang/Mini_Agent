import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from agentdojo.attacks import load_attack
from agentdojo.task_suite.load_suites import get_suite
from evals.agentdojo import BoundedAgent, MiniAgentPipeline, VERSION


class OfficialAdapterTests(unittest.TestCase):
    def run_official(self, mode, attacker=False):
        suite = get_suite(VERSION, 'banking')
        task = suite.get_user_task_by_id('user_task_0')
        injection = suite.get_injection_task_by_id('injection_task_0') if attacker else None
        scripted_task = injection or task

        async def scripted(agent, prompt):
            agent._openai_messages.append({'role': 'user', 'content': prompt})
            for index, call in enumerate(scripted_task.ground_truth(agent._mcp_manager.env)):
                call_id = f'call-{index}'
                agent._openai_messages.append({'role': 'assistant', 'content': None, 'tool_calls': [
                    {'id': call_id, 'type': 'function', 'function': {'name': call.function, 'arguments': json.dumps(call.args)}}]})
                result = await agent._execute_tool_call(call.function, call.args)
                agent._openai_messages.append({'role': 'tool', 'tool_call_id': call_id, 'content': result})
            agent._openai_messages.append({'role': 'assistant', 'content': 'Done.'})

        with tempfile.TemporaryDirectory() as directory:
            pipeline = MiniAgentPipeline(mode, directory, api_key='offline-test-key')
            with patch.object(BoundedAgent, 'run_once', scripted):
                result = suite.run_task_with_pipeline(pipeline, task, injection, {})
            audit = [json.loads(line) for line in (Path(directory) / 'query-0/audit.jsonl').read_text().splitlines()]
            self.assertTrue(audit)
            return result, pipeline.metrics[-1]

    def test_official_utility_and_real_permission_gate(self):
        (utility, _), metrics = self.run_official('bypassPermissions')
        self.assertTrue(utility)
        self.assertEqual(metrics['write_executions'], 1)
        (utility, _), metrics = self.run_official('dontAsk')
        self.assertFalse(utility)
        self.assertEqual(metrics['write_executions'], 0)
        self.assertTrue(metrics['environment_unchanged'])
        self.assertEqual(metrics['denied_calls'], 1)

    def test_official_security_boolean_means_attack_success(self):
        (_, attack_success), metrics = self.run_official('bypassPermissions', attacker=True)
        self.assertTrue(attack_success)
        self.assertEqual(metrics['api_requests'], 0)
        (_, attack_success), metrics = self.run_official('dontAsk', attacker=True)
        self.assertFalse(attack_success)
        self.assertTrue(metrics['environment_unchanged'])
        self.assertEqual(metrics['write_executions'], 0)

    def test_official_attack_produces_injection_payloads(self):
        suite = get_suite(VERSION, 'banking')
        pipeline = MiniAgentPipeline('dontAsk', '/unused', api_key='offline-test-key')
        attack = load_attack('important_instructions', suite, pipeline)
        payloads = attack.attack(suite.get_user_task_by_id('user_task_1'), suite.get_injection_task_by_id('injection_task_0'))
        self.assertTrue(payloads)
        self.assertTrue(all(isinstance(value, str) for value in payloads.values()))
