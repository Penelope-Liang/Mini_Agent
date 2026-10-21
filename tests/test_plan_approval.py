"""Plan approval behavior using real agent loops and mocked model responses."""

import asyncio
import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from agents.agent import Agent
from agents.sandbox_cli import run_task
from agents.terminal_approval import TerminalApproval

PLAN = 'Inspect app.py, update the parser, and run its tests.'


class PlanApprovalTests(unittest.IsolatedAsyncioTestCase):
    async def test_menu_shows_full_plan_and_reprompts(self):
        approval = TerminalApproval()
        with patch('agents.terminal_approval.print_plan_for_approval') as display, patch(
            'agents.terminal_approval.read_terminal_line', new=AsyncMock(side_effect=['', 'bad', '2', '', '2', 'Add tests'])
        ):
            self.assertEqual(await approval.plan(PLAN), {'choice': 'keep-planning', 'feedback': 'Add tests'})
            display.assert_called_once_with(PLAN)
        for answer, expected in [('1', 'execute'), ('3', 'exit'), (None, 'exit')]:
            with self.subTest(answer=answer), patch('agents.terminal_approval.read_terminal_line', new=AsyncMock(return_value=answer)):
                self.assertEqual((await approval.plan(PLAN))['choice'], expected)

    async def test_plan_wait_propagates_cancellation(self):
        approval = TerminalApproval()
        ready = asyncio.Event()

        async def wait(_):
            ready.set()
            await asyncio.Event().wait()

        with patch('agents.terminal_approval.read_terminal_line', wait):
            task = asyncio.create_task(approval.plan(PLAN))
            await ready.wait()
            task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await task
        self.assertTrue(approval.cancelled_while_waiting)

    async def test_plan_validation_revision_and_child_rejection(self):
        with tempfile.TemporaryDirectory() as directory:
            workspace = SimpleNamespace(output=Path(directory), active=True, normalize=lambda x: x)
            agent = Agent(api_key='test', model='test', custom_system_prompt='Test',
                          permission_mode='plan', workspace=workspace)
            self.addAsyncCleanup(agent._anthropic_client.close)
            approve = AsyncMock(return_value={'choice': 'keep-planning', 'feedback': 'Add tests'})
            agent.set_plan_approval_fn(approve)
            self.assertIsNone(agent._plan_file_path)
            self.assertNotIn(str(Path.home()), agent._system_prompt)
            for value in [None, '', 123, 'x' * 65537]:
                result = await agent._execute_tool_call('exit_plan_mode', {'plan': value})
                self.assertEqual(result.status, 'error')
            approve.assert_not_awaited()
            result = await agent._execute_tool_call('exit_plan_mode', {'plan': PLAN})
            self.assertIn('Add tests', result)
            self.assertEqual(agent.permission_mode, 'plan')
            self.assertEqual((workspace.output / 'plan.md').read_text(), PLAN)
            agent.is_sub_agent = True
            result = await agent._execute_tool_call('exit_plan_mode', {'plan': PLAN})
            self.assertEqual(result.status, 'denied')
            self.assertEqual(approve.await_count, 1)

    async def test_dont_ask_plan_exits_without_prompting(self):
        with tempfile.TemporaryDirectory() as directory:
            workspace = SimpleNamespace(output=Path(directory), active=True, normalize=lambda x: x)
            agent = Agent(api_key='test', model='test', custom_system_prompt='Test',
                          permission_mode='dontAsk', workspace=workspace)
            self.addAsyncCleanup(agent._anthropic_client.close)
            approve = AsyncMock(return_value={'choice': 'execute'})
            agent.set_plan_approval_fn(approve)
            await agent._execute_tool_call('enter_plan_mode', {})
            result = await agent._execute_tool_call('exit_plan_mode', {'plan': PLAN})
            approve.assert_not_awaited()
            self.assertEqual(result.status, 'stopped')
            self.assertEqual(agent._stop_reason, 'Plan not approved; exited without executing')
            self.assertEqual(agent.permission_mode, 'plan')
            self.assertEqual((workspace.output / 'plan.md').read_text(), PLAN)

    async def test_cli_exit_preserves_plan_and_stops_both_model_loops(self):
        for backend, timeout in (('openai', False), ('anthropic', False), ('openai', True)):
            with self.subTest(backend=backend, timeout=timeout), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                workspace = SimpleNamespace(
                    source=root, output=root, name='test', image='test', active=True,
                    start=AsyncMock(), close=AsyncMock(), execute=AsyncMock(),
                    export_changes=AsyncMock(return_value={'changed': []}), normalize=lambda x: x,
                )
                args = SimpleNamespace(workspace=str(root), output_dir=str(root), sandbox_image='test',
                    task_timeout=0.1 if timeout else 10, model='test', thinking=False, max_cost=None, max_turns=4,
                    skill=[], no_memory=True)
                calls = [('exit_plan_mode', {'plan': PLAN}),
                         ('write_file', {'file_path': 'app.py', 'content': 'unauthorized'})]
                if backend == 'openai':
                    response = {'choices': [{'message': {'role': 'assistant', 'content': None, 'tool_calls': [
                        {'id': str(i), 'type': 'function', 'function': {'name': name, 'arguments': json.dumps(inp)}}
                        for i, (name, inp) in enumerate(calls)]}}]}
                else:
                    response = SimpleNamespace(stop_reason='tool_use', usage=SimpleNamespace(input_tokens=1, output_tokens=1),
                        content=[SimpleNamespace(type='tool_use', id=str(i), name=name, input=inp)
                                 for i, (name, inp) in enumerate(calls)])
                model = AsyncMock(return_value=response)

                async def read(_prompt):
                    if timeout:
                        await asyncio.Event().wait()
                    return None
                with (
                    patch('agents.sandbox_cli.DockerWorkspace', return_value=workspace),
                    patch('agents.sandbox_cli.load_context', return_value={
                        'files': {}, 'skills': [], 'memories': [], 'allowed_tools': None, 'text': ''}),
                    patch.object(Agent, '_call_' + backend + '_stream', model),
                    patch('sys.stdin.isatty', return_value=False),
                    patch('agents.terminal_approval.read_terminal_line', read) if timeout else patch('sys.stdin.read', side_effect=AssertionError('Pipe read')),
                ):
                    with self.assertRaises(TimeoutError if timeout else RuntimeError):
                        await run_task(args, 'Plan changes', 'https://invalid.test/v1', 'test', backend == 'openai', 'plan')
                model.assert_awaited_once()
                workspace.execute.assert_not_awaited()
                report = json.loads((root / 'result.json').read_text())
                self.assertEqual(report['status'], 'stopped')
                self.assertEqual(report['stop_reason'], 'Task timed out while waiting for approval' if timeout else 'Plan not approved; exited without executing')
                self.assertEqual(report['plan'], PLAN)
                self.assertEqual((root / 'plan.md').read_text(), PLAN)
                workspace.close.assert_awaited_once()
