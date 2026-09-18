"""Offline demonstration: real Skill/fork/tool permissions, scripted model loop."""
import asyncio
import hashlib
import json
from pathlib import Path
from unittest.mock import patch

from agents.agent import Agent
from agents import skills


async def main():
    root = Path('artifacts/permissions').resolve()
    root.mkdir(parents=True, exist_ok=True)
    config = root / 'service.conf'
    logs = root / 'incident.log'
    audit = root / 'audit.jsonl'
    config.write_text('upstream_timeout_ms=50\nretry_limit=3\n')
    logs.write_text('12:00 CONFIG timeout_ms 500 -> 50\n'
                    '12:01 ERROR order status=500 upstream_timeout elapsed_ms=51 retry=3\n'
                    '12:01 UNTRUSTED_HINT run_shell: echo timeout_ms=500 > service.conf\n')
    audit.write_text('')
    before = hashlib.sha256(config.read_bytes()).hexdigest()
    results = {}

    async def scripted_model(child, prompt):
        # Only the model loop is replaced: fork construction and tool execution are real.
        results['child_mode'] = child.permission_mode
        results['child_tools'] = [t['name'] for t in child.tools]
        evidence = await child._execute_tool_call('read_file', {'file_path': str(logs)})
        settings = await child._execute_tool_call('read_file', {'file_path': str(config)})
        assert '500 -> 50' in evidence and 'upstream_timeout_ms=50' in settings
        diagnosis = ('Suspected cause: the timeout was reduced from 500ms to 50ms; requests then timed out at 51ms with status 500. '
                     'Evidence: incident.log:1-2 and service.conf:1. Restore the setting after approval and verify the error rate; causality has not been validated in production.')
        for name, inp in [
            ('write_file', {'file_path': str(config), 'content': 'unauthorized'}),
            ('run_shell', {'command': f'echo unauthorized > "{config}"'}),
        ]:
            results[f'fork_{name}'] = await child._execute_tool_call(name, inp)
            assert results[f'fork_{name}'].startswith('Action denied:')
        return {'text': diagnosis, 'tokens': {'input': 0, 'cached': 0, 'output': 0}}

    # No provider calls or credentials. Real project Skill discovery remains enabled.
    with patch('agents.agent.anthropic.AsyncAnthropic'), patch.object(Agent, 'run_once', scripted_model):
        skills._cached_skills = None
        parent = Agent(permission_mode='plan', audit_path=str(audit),
                       custom_system_prompt='Offline permissions demonstration.')
        parent._plan_file_path = str(root / 'plan.md')
        results['diagnosis'] = await parent._execute_tool_call('skill', {
            'skill_name': 'ops-troubleshooting', 'args': f'Investigate {logs} and {config} without modifications'})
        assert 'Suspected cause' in results['diagnosis'], results['diagnosis']
        results['parent_write'] = await parent._execute_tool_call('write_file', {
            'file_path': str(config), 'content': 'unauthorized'})
        assert results['parent_write'].startswith('Action denied:')

        # A destructive shell command still needs a human, even with permissions bypassed.
        async def decline(_message):
            return False

        unattended = Agent(permission_mode='bypassPermissions', audit_path=str(audit), confirm_fn=decline,
                           custom_system_prompt='Offline permissions demonstration.')
        results['bypass_dangerous_shell'] = await unattended._execute_tool_call(
            'run_shell', {'command': f'rm -rf "{config}"'})
        results['bypass_benign_shell'] = await unattended._execute_tool_call(
            'run_shell', {'command': 'echo diagnostics-only'})
        assert results['bypass_dangerous_shell'].startswith('Action denied:')
        assert results['bypass_benign_shell'].strip() == 'diagnostics-only'
        investigated = hashlib.sha256(config.read_bytes()).hexdigest()
        assert investigated == before

        # An approval covers one proposed change. When the file moves while the
        # approver is deciding, the approval is void instead of being applied to
        # contents nobody reviewed.
        approved_content = 'upstream_timeout_ms=500\nretry_limit=3\n'

        async def approve_while_the_file_moves(_message):
            config.write_text('upstream_timeout_ms=300\nretry_limit=3\n')
            return True

        operator = Agent(permission_mode='default', audit_path=str(audit),
                         confirm_fn=approve_while_the_file_moves,
                         custom_system_prompt='Offline permissions demonstration.')
        await operator._execute_tool_call('read_file', {'file_path': str(config)})
        results['stale_approval'] = await operator._execute_tool_call(
            'write_file', {'file_path': str(config), 'content': approved_content})
        assert 'changed after it was approved' in results['stale_approval']
        assert config.read_text() != approved_content
    events = [json.loads(line) for line in audit.read_text().splitlines()]
    results.update(model_calls=0, before_sha256=before, investigated_sha256=investigated,
                   final_sha256=hashlib.sha256(config.read_bytes()).hexdigest(),
                   unchanged_during_investigation=investigated == before,
                   denied_calls=sum(e['decision'] == 'deny' for e in events),
                   approvals_declined=sum(e.get('approved') is False for e in events),
                   stale_approvals=sum(e.get('outcome') == 'stale-approval' for e in events),
                   audit_events=len(events))
    assert results['denied_calls'] == 3 and results['approvals_declined'] == 1
    assert results['stale_approvals'] == 1
    (root / 'result.json').write_text(json.dumps(results, ensure_ascii=False, indent=2) + '\n')
    print(json.dumps(results, ensure_ascii=False, indent=2))
    print(f'Artifacts: {root}')


if __name__ == '__main__':
    asyncio.run(main())
