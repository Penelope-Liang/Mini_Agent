"""Offline integration tests of the real Agent tool execution boundary."""
import hashlib
import inspect
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

from agents import main
from agents.agent import Agent
from agents.tools import tool_definitions


class PermissionTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.file = self.root / 'service.conf'
        self.file.write_text('timeout=50\n')
        self.audit = self.root / 'audit.jsonl'
        for target in ('agents.agent.anthropic.AsyncAnthropic', 'agents.agent.build_system_prompt',
                       'agents.agent.Agent._refresh_runtime_system_prompt',
                       'agents.main.print_plan_for_approval', 'agents.main.print_plan_approval_options'):
            patcher = patch(target, return_value='' if target.endswith('build_system_prompt') else None)
            patcher.start()
            self.addCleanup(patcher.stop)
        patcher = patch('agents.tools._check_permission_rules', return_value=None)
        patcher.start()
        self.addCleanup(patcher.stop)

    def agent(self, mode='plan', names=None):
        agent = Agent(permission_mode='default', audit_path=str(self.audit),
                      custom_tools=None if names is None else [t for t in tool_definitions if t['name'] in names])
        agent.permission_mode = mode
        agent._plan_file_path = str(self.root / 'plan.md')
        agent.confirm_fn = AsyncMock(return_value=False)
        return agent

    async def read(self, agent):
        return await agent._execute_tool_call('read_file', {'file_path': str(self.file)})

    async def write(self, agent, path=None):
        return await agent._execute_tool_call('write_file', {'file_path': str(path or self.file), 'content': 'changed'})

    async def test_plan_blocks_writes_shell_and_skill_mutation_without_side_effects(self):
        agent = self.agent()
        self.assertIn('timeout=50', await self.read(agent))
        calls = [('write_file', {'file_path': str(self.file), 'content': 'changed'}),
                 ('edit_file', {'file_path': str(self.file), 'old_string': '50', 'new_string': '500'}),
                 ('run_shell', {'command': f'echo changed > "{self.file}"'}),
                 ('skill_create', {'name': 'unwanted', 'file_path': agent._plan_file_path}),
                 ('skill_evolve', {'skill_name': 'unwanted'})]
        with patch('agents.agent.execute_tool', new_callable=AsyncMock) as handler:
            for name, inp in calls:
                with self.subTest(tool=name):
                    self.assertIn('Action denied:', await agent._execute_tool_call(name, inp))
            handler.assert_not_called()
        self.assertEqual(self.file.read_text(), 'timeout=50\n')
        agent.confirm_fn.assert_not_called()

    async def test_allow_rule_cannot_override_plan(self):
        with patch('agents.tools._check_permission_rules', return_value='allow'):
            self.assertIn('Action denied:', await self.write(self.agent()))
        self.assertEqual(self.file.read_text(), 'timeout=50\n')

    async def test_plan_can_write_only_designated_plan(self):
        agent = self.agent()
        self.assertIn('Successfully', await self.write(agent, Path(agent._plan_file_path)))
        other = self.root / 'other.md'
        self.assertIn('Action denied:', await self.write(agent, other))
        self.assertFalse(other.exists())

    async def test_dontask_denies_existing_new_and_shell_without_prompt(self):
        agent = self.agent('dontAsk')
        await self.read(agent)
        self.assertIn('Action denied:', await self.write(agent))
        new = self.root / 'new.txt'
        self.assertIn('Action denied:', await self.write(agent, new))
        self.assertIn('Action denied:', await agent._execute_tool_call('run_shell', {'command': 'echo hello'}))
        self.assertFalse(new.exists())
        self.assertEqual(self.file.read_text(), 'timeout=50\n')
        agent.confirm_fn.assert_not_called()

    async def test_default_requires_fresh_approval_each_write(self):
        agent = self.agent('default')
        agent.confirm_fn.side_effect = [True, False]
        await self.read(agent)
        self.assertIn('Successfully', await self.write(agent))
        self.assertIn('Action denied:', await agent._execute_tool_call('write_file', {'file_path': str(self.file), 'content': 'second'}))
        self.assertEqual(self.file.read_text(), 'changed')
        self.assertEqual(agent.confirm_fn.await_count, 2)

    async def test_accept_edits_requires_read_and_rejects_stale_read(self):
        agent = self.agent('acceptEdits')
        self.assertIn('must read', await self.write(agent))
        self.assertEqual(self.file.read_text(), 'timeout=50\n')
        await self.read(agent)
        old = self.file.stat().st_mtime
        self.file.write_text('external change')
        os.utime(self.file, (old + 10, old + 10))
        self.assertIn('modified externally', await self.write(agent))
        self.assertEqual(self.file.read_text(), 'external change')
        await self.read(agent)
        self.assertIn('Successfully', await self.write(agent))

    async def test_approval_prompt_shows_the_file_and_both_versions(self):
        agent = self.agent('default')
        agent.confirm_fn.return_value = True
        await self.read(agent)
        await agent._execute_tool_call('edit_file', {'file_path': str(self.file),
                                                    'old_string': 'timeout=50', 'new_string': 'timeout=500'})
        prompt = agent.confirm_fn.await_args.args[0]
        for expected in (str(self.file), '- timeout=50', '+ timeout=500'):
            self.assertIn(expected, prompt)

    async def test_approval_is_void_when_the_file_changes_after_approval(self):
        for tool, extra in (('write_file', {'content': 'approved'}),
                            ('edit_file', {'old_string': 'timeout=50', 'new_string': 'timeout=500'})):
            with self.subTest(tool=tool):
                self.file.write_text('timeout=50\n')
                agent = self.agent('default')
                await self.read(agent)
                # The approver takes time, and the file moves while they decide.
                def approve_while_the_file_moves(_message):
                    self.file.write_text('timeout=300\n')
                    return True

                agent.confirm_fn.side_effect = approve_while_the_file_moves
                result = await agent._execute_tool_call(tool, {'file_path': str(self.file), **extra})
                self.assertIn('changed after it was approved', result)
                self.assertEqual(self.file.read_text(), 'timeout=300\n')
                outcomes = [json.loads(line)['outcome'] for line in self.audit.read_text().splitlines()
                            if 'outcome' in json.loads(line)]
                self.assertIn('stale-approval', outcomes)

    async def test_execution_writes_exactly_the_approved_content(self):
        agent = self.agent('default')
        agent.confirm_fn.return_value = True
        await self.read(agent)
        self.assertIn('Successfully', await agent._execute_tool_call(
            'edit_file', {'file_path': str(self.file), 'old_string': 'timeout=50', 'new_string': 'timeout=500'}))
        self.assertEqual(self.file.read_text(), 'timeout=500\n')
        events = [json.loads(line) for line in self.audit.read_text().splitlines()]
        approved = next(e['proposal'] for e in events if e.get('proposal'))
        self.assertEqual(approved['after_hash'], hashlib.sha256(b'timeout=500\n').hexdigest())
        self.assertTrue(next(e['wrote_approved_content'] for e in events if 'wrote_approved_content' in e))

    async def test_plan_approval_does_not_grant_blanket_edit_permission(self):
        agent = self.agent()
        agent._plan_approval_fn = AsyncMock(return_value={'choice': 'execute'})
        await agent._execute_tool_call('exit_plan_mode', {})
        self.assertEqual(agent.permission_mode, 'default')
        await self.read(agent)
        self.assertIn('Action denied:', await self.write(agent))
        agent.confirm_fn.assert_awaited_once()
        self.assertEqual(self.file.read_text(), 'timeout=50\n')

    async def test_empty_or_readonly_tools_bind_even_bypass_mode(self):
        for names in ([], ['read_file']):
            agent = self.agent('bypassPermissions', names)
            self.assertEqual([t['name'] for t in agent.tools], names)
            self.assertIn('Action denied:', await self.write(agent))
        self.assertEqual(self.file.read_text(), 'timeout=50\n')

    async def test_dangerous_shell_still_needs_a_human_under_bypass(self):
        agent = self.agent('bypassPermissions', ['run_shell'])
        with patch('agents.agent.execute_tool', new_callable=AsyncMock) as handler:
            self.assertIn('Action denied:', await agent._execute_tool_call(
                'run_shell', {'command': f'rm -rf "{self.root}"'}))
            handler.assert_not_called()
        self.assertEqual(agent.confirm_fn.await_count, 1)
        self.assertEqual(self.file.read_text(), 'timeout=50\n')
        # A benign command in the same mode still runs without prompting.
        with patch('agents.agent.execute_tool', new_callable=AsyncMock, return_value='ok') as handler:
            self.assertEqual(await agent._execute_tool_call('run_shell', {'command': 'echo hello'}), 'ok')
            handler.assert_called_once()
        self.assertEqual(agent.confirm_fn.await_count, 1)

    async def test_dangerous_shell_floor_outranks_allow_rule_and_dontask(self):
        for mode, expected_prompts in (('default', 1), ('acceptEdits', 1), ('dontAsk', 0)):
            with self.subTest(mode=mode):
                agent = self.agent(mode, ['run_shell'])
                with patch('agents.tools._check_permission_rules', return_value='allow'), \
                     patch('agents.agent.execute_tool', new_callable=AsyncMock) as handler:
                    self.assertIn('Action denied:', await agent._execute_tool_call(
                        'run_shell', {'command': 'sudo reboot'}))
                    handler.assert_not_called()
                self.assertEqual(agent.confirm_fn.await_count, expected_prompts)

    async def test_unclassified_mcp_denied_in_plan_without_dispatch(self):
        agent = self.agent()
        agent.tools.append({'name': 'external_mutate'})
        agent._mcp_manager.call_tool = AsyncMock()
        self.assertIn('Action denied:', await agent._execute_tool_call('external_mutate', {}))
        agent._mcp_manager.call_tool.assert_not_called()

    async def test_plan_exit_requires_explicit_approval(self):
        agent = self.agent()
        self.assertIn('Action denied:', await agent._execute_tool_call('exit_plan_mode', {}))
        self.assertEqual(agent.permission_mode, 'plan')
        # The REPL callback is a coroutine function, so a synchronous fake would hide a missing await.
        agent._plan_approval_fn = AsyncMock(return_value={'choice': 'keep-planning', 'feedback': 'not yet'})
        self.assertIn('keep planning', await agent._execute_tool_call('exit_plan_mode', {}))
        self.assertEqual(agent.permission_mode, 'plan')
        agent._plan_approval_fn = AsyncMock(return_value={'choice': 'unrecognized'})
        self.assertIn('Action denied:', await agent._execute_tool_call('exit_plan_mode', {}))
        self.assertEqual(agent.permission_mode, 'plan')
        agent._plan_approval_fn = AsyncMock(return_value={'choice': 'execute'})
        self.assertIn('approved', await agent._execute_tool_call('exit_plan_mode', {}))
        self.assertEqual(agent.permission_mode, 'default')

    async def test_repl_callback_satisfies_the_agent_approval_contract(self):
        agent = self.agent()
        with patch('agents.main.signal.signal'), patch('builtins.input', side_effect=EOFError):
            await main.run_repl(agent)
        approve = agent._plan_approval_fn
        # The bug this guards: a synchronous callback would leave the Agent holding a coroutine.
        self.assertTrue(inspect.iscoroutinefunction(approve))
        with patch('builtins.input', side_effect=['1', '2', '3', '4', 'why', EOFError]):
            choices = [(await approve('plan.md'))['choice'] for _ in range(5)]
        self.assertEqual(choices, ['clear-and-execute', 'execute', 'manual-execute',
                                   'keep-planning', 'keep-planning'])
        # Only an explicit approval leaves read-only: EOF and rejection both keep plan mode.
        with patch('builtins.input', side_effect=[EOFError, '4', 'revise', '2']):
            self.assertIn('No approval received', await agent._execute_tool_call('exit_plan_mode', {}))
            self.assertEqual(agent.permission_mode, 'plan')
            self.assertIn('revise', await agent._execute_tool_call('exit_plan_mode', {}))
            self.assertEqual(agent.permission_mode, 'plan')
            self.assertIn('approved', await agent._execute_tool_call('exit_plan_mode', {}))
        self.assertEqual(agent.permission_mode, 'default')

    async def test_skill_fork_inherits_mode_and_intersects_tools(self):
        for mode in ('plan', 'dontAsk', 'default', 'acceptEdits'):
            for allowed in ([], ['read_file', 'write_file']):
                parent = self.agent(mode, ['skill', 'read_file'])
                async def run(child, prompt):
                    self.assertEqual(child.permission_mode, mode)
                    self.assertEqual([t['name'] for t in child.tools], [] if not allowed else ['read_file'])
                    self.assertIn('Action denied:', await self.write(child))
                    return {'text': 'verified', 'tokens': {'input': 0, 'cached': 0, 'output': 0}}
                skill = {'context': 'fork', 'allowed_tools': allowed, 'prompt': 'offline test'}
                with patch('agents.skills.execute_skill', return_value=skill), patch.object(Agent, 'run_once', run), \
                     patch.object(Agent, '_generate_plan_file_path', return_value=str(self.root / 'child-plan.md')):
                    self.assertEqual(await parent._execute_tool_call('skill', {'skill_name': 'test'}), 'verified')
        self.assertEqual(self.file.read_text(), 'timeout=50\n')

    async def test_general_child_cannot_expand_parent_tools(self):
        parent = self.agent('dontAsk', ['agent', 'read_file'])
        async def run(child, prompt):
            self.assertEqual(child.permission_mode, 'dontAsk')
            self.assertTrue({t['name'] for t in child.tools} <= {'agent', 'read_file'})
            self.assertIn('Action denied:', await self.write(child))
            return {'text': 'verified', 'tokens': {'input': 0, 'cached': 0, 'output': 0}}
        with patch.object(Agent, 'run_once', run):
            self.assertEqual(await parent._execute_tool_call('agent', {'prompt': 'test'}), 'verified')

    async def test_audit_records_allow_denial_and_execution_failure(self):
        agent = self.agent()
        await self.read(agent)
        await self.write(agent)
        agent.permission_mode = 'acceptEdits'
        agent._read_file_state.clear()
        await self.write(agent)
        events = [json.loads(line) for line in self.audit.read_text().splitlines()]
        self.assertTrue(any(e['decision'] == 'deny' for e in events))
        self.assertEqual([e['outcome'] for e in events if e['phase'] == 'completion'], ['completed', 'failed'])
        self.assertTrue(all(e['session_id'] == agent.session_id for e in events))

    async def test_unwritable_audit_fails_closed(self):
        agent = self.agent('acceptEdits')
        await self.read(agent)
        agent.audit_path = self.root  # a directory, cannot append
        self.assertIn('Error executing tool:', await self.write(agent))
        self.assertEqual(self.file.read_text(), 'timeout=50\n')

    async def test_binary_and_eof_reads_do_not_authorize_overwrite(self):
        agent = self.agent('acceptEdits')
        await agent._execute_tool_call('read_file', {'file_path': str(self.file), 'offset': 999})
        self.assertIn('must read', await self.write(agent))
        self.file.write_bytes(b'\x00binary')
        await self.read(agent)
        self.assertIn('must read', await self.write(agent))
        self.assertEqual(self.file.read_bytes(), b'\x00binary')
