import unittest
from unittest.mock import patch

from agents.agent import Agent


class SkillQueryTests(unittest.TestCase):
    def test_explicit_query_does_not_replace_task_payload(self):
        agent = Agent.__new__(Agent)
        hit = {'all_hits': [{'name': 'example_skill'}]}
        with patch('agents.skills.format_retrieved_skill_context', return_value=('CHECKLIST', hit)) as retrieve:
            text, reference = agent._augment_user_message_with_skill_context('large log payload', query='Diagnose service')
        retrieve.assert_called_once_with('Diagnose service', limit=3)
        self.assertIn('large log payload', text)
        self.assertIn('CHECKLIST', text)
        self.assertIs(reference, hit)


class RequestCountTests(unittest.IsolatedAsyncioTestCase):
    async def test_text_and_failed_dispatches_count_without_changing_tools(self):
        agent = Agent.__new__(Agent)
        agent.tools = [{'name': 'read_file'}]
        original = agent.tools

        async def text_reply(**kwargs):
            self.assertEqual(kwargs, {'value': 3})
            return {'text': 'result'}

        self.assertEqual(await agent._dispatch_model_request(text_reply, value=3), {'text': 'result'})

        async def failed():
            raise RuntimeError('failed request')

        with self.assertRaises(RuntimeError):
            await agent._dispatch_model_request(failed)
        self.assertEqual(agent.model_requests, 2)
        self.assertIs(agent.tools, original)
