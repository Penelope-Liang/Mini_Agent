"""Terminal approval and sandbox deadline regression tests; no model requests."""

import asyncio
import json
import os
import pty
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from agents.agent import Agent
from agents.sandbox_cli import run_task
from agents.terminal_approval import TerminalApproval


class TerminalApprovalTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.master, slave = pty.openpty()
        self.stdin = os.fdopen(slave, 'r')
        self.addCleanup(os.close, self.master)
        self.addCleanup(self.stdin.close)
        patcher = patch('sys.stdin', self.stdin)
        patcher.start()
        self.addCleanup(patcher.stop)

    async def test_explicit_answers_and_eof(self):
        for value, expected in [(b'y\n', True), (b'YES\n', True), (b'\n', False),
                                (b'no\n', False), (b'yesterday\n', False), (b'\x04', False)]:
            with self.subTest(value=value):
                task = asyncio.create_task(TerminalApproval()('Test'))
                await asyncio.sleep(0)
                os.write(self.master, value)
                self.assertEqual(await asyncio.wait_for(task, 1), expected)

    async def test_cancelled_reader_does_not_consume_next_answer(self):
        approval = TerminalApproval()
        task = asyncio.create_task(approval('First'))
        await asyncio.sleep(0)
        loop = asyncio.get_running_loop()
        with patch.object(loop, 'remove_reader', wraps=loop.remove_reader) as remove:
            task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await task
            remove.assert_called_once_with(self.stdin.fileno())
        self.assertTrue(approval.cancelled_while_waiting)
        with patch('agents.terminal_approval.os.read', side_effect=AssertionError('Stale reader')):
            os.write(self.master, b'yes\n')
            await asyncio.sleep(0.02)
        self.assertTrue(await asyncio.wait_for(TerminalApproval()('Next'), 1))

    async def test_plan_eof_exits_without_approval(self):
        task = asyncio.create_task(TerminalApproval().plan('Inspect the parser.'))
        await asyncio.sleep(0)
        os.write(self.master, b'\x04')
        self.assertEqual(await asyncio.wait_for(task, 1), {'choice': 'exit'})

    async def test_pipe_is_rejected_without_reading(self):
        read_fd, write_fd = os.pipe()
        with os.fdopen(read_fd, 'r') as stream:
            try:
                os.write(write_fd, b'y\n')
                with patch('sys.stdin', stream):
                    self.assertFalse(await TerminalApproval()('Test'))
                self.assertEqual(os.read(read_fd, 2), b'y\n')
            finally:
                os.close(write_fd)

    async def test_cli_timeout_and_cancellation_preserve_reason_and_prevent_execution(self):
        for waiting, cancel in ((True, False), (False, False), (True, True), (False, True)):
            with self.subTest(waiting=waiting, cancel=cancel), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                source = root / 'source'
                source.mkdir()
                workspace = SimpleNamespace(
                    source=source, output=root / 'report', name='test', image='test', active=True,
                    start=AsyncMock(), close=AsyncMock(), execute=AsyncMock(),
                    normalize=lambda inp: inp,
                )
                workspace.output.mkdir()
                args = SimpleNamespace(workspace=str(source), output_dir=str(root), sandbox_image='test',
                    task_timeout=10 if cancel else 0.15, model='test', thinking=False, max_cost=None, max_turns=4,
                    skill=[], no_memory=True)
                ready = asyncio.Event()
                original_approval = TerminalApproval.__call__

                async def approval(approver, description):
                    ready.set()
                    return await original_approval(approver, description)

                async def model_loop(agent, _prompt):
                    if waiting:
                        await agent._execute_tool_call('run_shell', {'command': 'echo approved'})
                    else:
                        ready.set()
                        await asyncio.Event().wait()

                with (
                    patch('agents.sandbox_cli.DockerWorkspace', return_value=workspace),
                    patch('agents.sandbox_cli.load_context', return_value={
                        'files': {}, 'skills': [], 'memories': [], 'allowed_tools': None, 'text': ''}),
                    patch.object(Agent, '_chat_openai', model_loop),
                    patch.object(TerminalApproval, '__call__', approval),
                    patch('agents.agent.check_permission', return_value={'action': 'confirm', 'message': 'Test'}),
                ):
                    task = asyncio.create_task(run_task(
                        args, 'Test', 'https://invalid.test/v1', 'test', True, 'default'))
                    try:
                        if cancel:
                            await asyncio.wait_for(ready.wait(), 2)
                            task.cancel()
                        with self.assertRaises(asyncio.CancelledError if cancel else TimeoutError):
                            await task
                    finally:
                        if not task.done():
                            task.cancel()
                            await asyncio.gather(task, return_exceptions=True)
                workspace.execute.assert_not_awaited()
                workspace.close.assert_awaited_once()
                report = json.loads((workspace.output / 'result.json').read_text())
                self.assertEqual(report['status'], 'stopped')
                reason = 'Task interrupted' if cancel else 'Task timed out'
                self.assertEqual(report['stop_reason'], reason + (' while waiting for approval' if waiting else ''))
                audit = workspace.output / 'audit.jsonl'
                events = [json.loads(line) for line in audit.read_text().splitlines()] if audit.exists() else []
                cancelled = [e for e in events if e.get('outcome') == 'cancelled-while-waiting']
                if waiting:
                    self.assertEqual(len(cancelled), 1)
                    self.assertEqual((cancelled[0]['tool'], cancelled[0]['phase'], cancelled[0]['approved']),
                                     ('run_shell', 'authorization', False))
                    self.assertFalse([e for e in events if e.get('phase') == 'completion'])
                else:
                    self.assertEqual(cancelled, [])

    async def test_cli_preserves_existing_stop_reasons_and_exports(self):
        for reason in ('repeated_result for read_file (4 observations)', 'Turn limit reached (4 >= 4)'):
            with self.subTest(reason=reason), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                workspace = SimpleNamespace(
                    source=root, output=root, name='test', image='test', active=True,
                    start=AsyncMock(), close=AsyncMock(),
                    export_changes=AsyncMock(return_value={'changed': ['app.py']}),
                )
                args = SimpleNamespace(workspace=str(root), output_dir=str(root), sandbox_image='test',
                    task_timeout=10, model='test', thinking=False, max_cost=None, max_turns=4,
                    skill=[], no_memory=True)
                with (
                    patch('agents.sandbox_cli.DockerWorkspace', return_value=workspace),
                    patch('agents.sandbox_cli.load_context', return_value={
                        'files': {}, 'skills': [], 'memories': [], 'allowed_tools': None, 'text': ''}),
                    patch.object(Agent, 'run_once', new=AsyncMock(return_value={
                        'status': 'stopped', 'stop_reason': reason, 'text': 'Partial result'})),
                ):
                    with self.assertRaisesRegex(RuntimeError, 'Task stopped before completion'):
                        await run_task(args, 'Test', 'https://invalid.test/v1', 'test', True, 'default')
                report = json.loads((root / 'result.json').read_text())
                self.assertEqual(report['status'], 'stopped')
                self.assertEqual(report['stop_reason'], reason)
                self.assertEqual(report['changes'], {'changed': ['app.py']})
                workspace.export_changes.assert_awaited_once()
                workspace.close.assert_awaited_once()

    async def test_local_cancelled_approval_is_audited(self):
        with tempfile.TemporaryDirectory() as directory:
            audit = Path(directory) / 'audit.jsonl'
            waiting = asyncio.Event()

            async def confirm(_message):
                waiting.set()
                await asyncio.Event().wait()

            agent = Agent(api_key='test', model='test', is_sub_agent=True, custom_system_prompt='Test',
                          audit_path=str(audit), confirm_fn=confirm)
            self.addAsyncCleanup(agent._anthropic_client.close)
            with patch('agents.agent.check_permission', return_value={'action': 'confirm', 'message': 'Test'}), \
                 patch.object(agent, '_dispatch_tool_call', new=AsyncMock()) as dispatch:
                task = asyncio.create_task(agent._execute_tool_call('run_shell', {'command': 'echo hi'}))
                await asyncio.wait_for(waiting.wait(), 1)
                task.cancel()
                with self.assertRaises(asyncio.CancelledError):
                    await task
            dispatch.assert_not_awaited()
            events = [json.loads(line) for line in audit.read_text().splitlines()]
            self.assertEqual([(e['phase'], e['approved'], e['outcome']) for e in events],
                             [('authorization', False, 'cancelled-while-waiting')])

    async def test_sandbox_children_do_not_inherit_approval(self):
        with tempfile.TemporaryDirectory() as directory:
            workspace = SimpleNamespace(output=Path(directory))
            confirm = AsyncMock(return_value=True)
            parent = Agent(api_key='test', model='test', is_sub_agent=True,
                           custom_system_prompt='Test', workspace=workspace, confirm_fn=confirm)
            self.addAsyncCleanup(parent._anthropic_client.close)
            seen = []

            async def run_child(child, _prompt):
                self.assertIsNone(child.confirm_fn)
                self.assertIs(child.workspace, workspace)
                with patch('builtins.input', side_effect=AssertionError('Blocking input')):
                    self.assertFalse(await child._confirm_dangerous('Test'))
                seen.append(child)
                return {'status': 'completed', 'text': 'Done',
                        'tokens': {'input': 0, 'cached': 0, 'output': 0}}

            with patch.object(Agent, 'run_once', run_child), patch('agents.skills.execute_skill', return_value={
                'context': 'fork', 'allowed_tools': ['read_file'], 'prompt': 'Test',
            }):
                await parent._execute_agent_tool({'type': 'explore', 'prompt': 'Test'})
                await parent._execute_skill_tool({'skill_name': 'test'})
            self.assertEqual(len(seen), 2)
            confirm.assert_not_awaited()


class KeyboardInterruptTests(unittest.TestCase):
    # KeyboardInterrupt escapes the event loop, so it needs its own asyncio.run.
    def test_direct_keyboard_interrupt_reports_interrupted(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            workspace = SimpleNamespace(
                source=root, output=root, name='test', image='test', active=True,
                start=AsyncMock(), close=AsyncMock(), execute=AsyncMock(),
            )
            args = SimpleNamespace(workspace=str(root), output_dir=str(root), sandbox_image='test',
                task_timeout=10, model='test', thinking=False, max_cost=None, max_turns=4,
                skill=[], no_memory=True)
            with (
                patch('agents.sandbox_cli.DockerWorkspace', return_value=workspace),
                patch('agents.sandbox_cli.load_context', return_value={
                    'files': {}, 'skills': [], 'memories': [], 'allowed_tools': None, 'text': ''}),
                patch.object(Agent, 'run_once', new=AsyncMock(side_effect=KeyboardInterrupt)),
            ):
                with self.assertRaises(KeyboardInterrupt):
                    asyncio.run(run_task(args, 'Test', 'https://invalid.test/v1', 'test', True, 'default'))
            workspace.execute.assert_not_awaited()
            workspace.close.assert_awaited_once()
            report = json.loads((root / 'result.json').read_text())
            self.assertEqual((report['status'], report['stop_reason']), ('stopped', 'Task interrupted'))
