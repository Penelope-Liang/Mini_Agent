"""Finalization failures must not discard partial results or skip cleanup."""
import asyncio
import json
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from agents.sandbox_cli import _finalize_task


class FinalizationTests(unittest.IsolatedAsyncioTestCase):
    async def test_export_failure_timeout_and_unavailable_preserve_stop(self):
        for mode in ('failure', 'timeout', 'unavailable', 'success'):
            with self.subTest(mode=mode), tempfile.TemporaryDirectory() as directory:
                order = []

                async def export():
                    order.append('export')
                    if mode == 'failure':
                        raise OSError('snapshot failed')
                    if mode == 'timeout':
                        await asyncio.Event().wait()
                    return {'changed': ['app.py']}

                async def close():
                    order.append('close')
                    raise OSError('cleanup failed')

                workspace = SimpleNamespace(
                    active=mode != 'unavailable', output=Path(directory),
                    export_changes=AsyncMock(side_effect=export), close=AsyncMock(side_effect=close),
                    context7=SimpleNamespace(close=AsyncMock()),
                )
                client = SimpleNamespace(close=AsyncMock(side_effect=OSError('client failed')))
                agent = SimpleNamespace(
                    plan_content=None, get_token_usage=lambda: {}, _get_current_cost_usd=lambda: 0,
                    _openai_messages=[], _anthropic_messages=[], _openai_client=client,
                )
                status = {'status': 'stopped', 'stop_reason': 'alternating_cycle'}
                with patch('agents.sandbox_cli.EXPORT_TIMEOUT', 0.01):
                    await _finalize_task(workspace, agent, status, time.monotonic(), True)
                report = json.loads((workspace.output / 'result.json').read_text())
                self.assertEqual(report['status'], 'stopped')
                self.assertEqual(report['stop_reason'], 'alternating_cycle')
                self.assertEqual(report['export_status'], {
                    'success': 'succeeded', 'unavailable': 'skipped',
                    'failure': 'failed', 'timeout': 'failed',
                }[mode])
                self.assertEqual(report['patch_generated'], mode == 'success')
                self.assertEqual(order, ['close'] if mode == 'unavailable' else ['export', 'close'])
                self.assertEqual(set(report['cleanup_errors']), {'workspace', 'model_client'})
                workspace.context7.close.assert_awaited_once()
                if mode != 'success':
                    self.assertTrue(report['export_error'])

    async def test_execution_and_verification_status_are_independent(self):
        from agents.sandbox_cli import run_task

        for outcome in ('passed', 'failed', 'error', 'not_run'):
            with self.subTest(outcome=outcome), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                workspace = SimpleNamespace(
                    source=root, output=root, name='test', image='test', active=True,
                    start=AsyncMock(), close=AsyncMock(),
                    export_changes=AsyncMock(return_value={'changed': ['app.py']}),
                )
                client = SimpleNamespace(close=AsyncMock())
                agent = SimpleNamespace(
                    plan_content=None, get_token_usage=lambda: {}, _get_current_cost_usd=lambda: 0,
                    _openai_messages=[], _anthropic_messages=[], _openai_client=client,
                    set_plan_approval_fn=lambda fn: None,
                    run_once=AsyncMock(return_value={
                        'status': 'completed', 'text': 'All tests passed!',
                        'verification_status': 'passed',
                    }),
                )
                args = SimpleNamespace(
                    workspace=str(root), output_dir=str(root), sandbox_image='test', task_timeout=10,
                    model='test', thinking=False, max_cost=None, max_turns=4,
                    protect_path=['tests'], verify_command=None if outcome == 'not_run' else 'python check.py',
                    verify_timeout=10,
                )
                with (
                    patch('agents.sandbox_cli.DockerWorkspace', return_value=workspace),
                    patch('agents.sandbox_cli.Agent', return_value=agent),
                    patch('agents.sandbox_cli.load_context', return_value={
                        'files': {}, 'skills': [], 'memories': [], 'allowed_tools': None, 'text': ''}),
                    patch('agents.sandbox_cli.verify_candidate', new=AsyncMock(
                        return_value={'status': outcome})) as verify,
                ):
                    if outcome in ('failed', 'error'):
                        with self.assertRaisesRegex(RuntimeError, 'verification did not pass'):
                            await run_task(args, 'Fix', None, 'test', True, 'default')
                    else:
                        await run_task(args, 'Fix', None, 'test', True, 'default')
                report = json.loads((root / 'result.json').read_text())
                self.assertEqual(report['execution_status'], 'completed')
                self.assertEqual(report['status'], 'completed')
                self.assertEqual(report['verification_status'], outcome)
                if outcome == 'not_run':
                    verify.assert_not_awaited()
                else:
                    verify.assert_awaited_once_with(workspace, 'python check.py', 10)
                workspace.close.assert_awaited_once()
