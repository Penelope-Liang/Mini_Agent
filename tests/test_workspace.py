"""Snapshot validation tests and opt-in real Docker boundary tests.

MINIAGENT_DOCKER_TESTS=1 python -m unittest tests.test_workspace -v
"""

import base64
import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest
from unittest.mock import AsyncMock, patch

from agents.agent import Agent
from agents.workspace import DockerWorkspace, snapshot_source, validate_snapshot


class SnapshotTests(unittest.TestCase):
    def test_source_omits_secrets_ignored_files_and_links(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            subprocess.run(["git", "init", "-q", str(root)], check=True)
            (root / ".gitignore").write_text("ignored.txt\n")
            (root / ".sandboxignore").write_text("data/\n")
            (root / "data").mkdir()
            (root / "data" / "large.json").write_text("excluded")
            for name in ["app.py", ".env", ".env.production", "private.key", "ignored.txt"]:
                (root / name).write_text("test")
            (root / "link.py").symlink_to(root / "app.py")
            entries = snapshot_source(root)
            self.assertEqual(set(entries), {"app.py", ".gitignore", ".sandboxignore"})

    def test_export_rejects_unsafe_paths_and_sizes(self):
        entry = {"content": base64.b64encode(b"data").decode(), "executable": False}
        for name in ["../escape", "/absolute", "a/../../escape", ".git/config", "x\\y", "a\nb", "a//b"]:
            with self.subTest(name=name), self.assertRaises(ValueError):
                validate_snapshot({name: entry})
        with self.assertRaises(ValueError):
            validate_snapshot({"app.py": {"content": "invalid!", "executable": False}})

    def test_results_cannot_be_inside_source(self):
        with tempfile.TemporaryDirectory() as directory:
            with self.assertRaises(ValueError):
                DockerWorkspace(Path(directory), Path(directory) / "results")


class LifecycleTests(unittest.IsolatedAsyncioTestCase):
    async def test_docker_failure_never_executes_locally(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "source"
            source.mkdir()
            (source / "app.py").write_text("original")
            workspace = DockerWorkspace(source, root / "results")
            with patch.object(
                workspace, "_docker", new=AsyncMock(side_effect=RuntimeError("No such container"))
            ):
                with self.assertRaises(RuntimeError):
                    await workspace.start()
                with self.assertRaises(RuntimeError):
                    await workspace.execute("run_shell", {"command": "touch surprise"}, {})
            self.assertFalse(workspace.active)
            self.assertEqual((source / "app.py").read_text(), "original")
            self.assertFalse((source / "surprise").exists())


@unittest.skipUnless(os.environ.get("MINIAGENT_DOCKER_TESTS") == "1", "requires explicit Docker test opt-in")
class DockerTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        self.source = self.root / "source"
        self.source.mkdir()
        (self.source / "app.py").write_text("value = 1\n")
        (self.source / ".env").write_text("PRIVATE_TOKEN=not-for-container")
        (self.root / "sentinel").write_text("KEEP_ME")
        self.workspace = DockerWorkspace(self.source, self.root / "results")
        await self.workspace.start()
        self.addAsyncCleanup(self.workspace.close)
        self.state = {}

    async def shell(self, command, **extra):
        return await self.workspace.execute("run_shell", {"command": command, **extra}, self.state)

    async def test_edit_export_and_apply_without_changing_original(self):
        await self.workspace.execute("read_file", {"file_path": "app.py"}, self.state)
        result = await self.workspace.execute(
            "edit_file",
            {"file_path": "app.py", "old_string": "value = 1", "new_string": "value = 2"},
            self.state,
        )
        self.assertIn("Successfully", result)
        await self.shell('python -c "from app import value; assert value == 2"')
        await self.workspace.execute("write_file", {"file_path": "new.txt", "content": "new\n"}, self.state)
        # A second task receives the untouched source, not the first task's edits.
        other = DockerWorkspace(self.source, self.root / "results")
        await other.start()
        try:
            self.assertNotEqual(other.name, self.workspace.name)
            text = await other.execute("read_file", {"file_path": "app.py"}, {})
            self.assertIn("value = 1", text)
        finally:
            await other.close()
        exported = await self.workspace.export_changes()
        self.assertEqual(exported["changed"], ["app.py", "new.txt"])
        self.assertEqual((self.source / "app.py").read_text(), "value = 1\n")
        apply_dir = self.root / "apply"
        shutil.copytree(self.source, apply_dir)
        subprocess.run(
            ["git", "apply", "-p2", str(self.workspace.output / "changes.patch")],
            cwd=apply_dir,
            check=True,
            capture_output=True,
        )
        self.assertEqual((apply_dir / "app.py").read_text(), "value = 2\n")
        self.assertEqual((apply_dir / "new.txt").read_text(), "new\n")

    async def test_paths_symlinks_and_host_access(self):
        for path in ["../sentinel", str(self.root / "sentinel"), "/etc/passwd"]:
            with self.subTest(path=path), self.assertRaises(ValueError):
                await self.workspace.execute("read_file", {"file_path": path}, self.state)
        await self.shell("ln -s /etc/passwd link")
        with self.assertRaisesRegex(ValueError, "Symbolic"):
            await self.workspace.execute("read_file", {"file_path": "link"}, self.state)
        # No dangerous-pattern check is involved: Python attempts the actual write.
        command = "python -c " + __import__("shlex").quote(
            f"from pathlib import Path; Path({str(self.root / 'sentinel')!r}).write_text('BAD')"
        )
        self.assertIn("Command failed", await self.shell(command))
        self.assertEqual((self.root / "sentinel").read_text(), "KEEP_ME")
        exported = await self.workspace.export_changes()
        self.assertNotIn("link", exported["changed"])

    async def test_container_configuration_and_credentials(self):
        config = json.loads(await self.workspace._docker("inspect", self.workspace.name))[0]
        self.assertEqual(config["HostConfig"]["NetworkMode"], "none")
        self.assertTrue(config["HostConfig"]["ReadonlyRootfs"])
        self.assertEqual(config["Config"]["User"], "10001:10001")
        self.assertFalse(any(m["Type"] == "bind" for m in config["Mounts"]))
        self.assertIn("ALL", config["HostConfig"]["CapDrop"])
        self.assertEqual(config["HostConfig"]["PidsLimit"], 64)
        with patch.dict(os.environ, {"APIKEY": "host-only-test-secret", "GH_TOKEN": "host-only-test-token"}):
            output = await self.shell(
                "python -c \"import os; print(os.getenv('APIKEY')); print(os.getenv('GH_TOKEN'))\""
            )
        self.assertEqual(output.strip(), "None\nNone")
        self.assertIn("Command failed", await self.shell("cat .env"))
        self.assertIn("Command failed", await self.shell("touch /opt/not-allowed"))
        output = await self.shell(
            "python -c \"import socket; s=socket.socket(); s.settimeout(1); s.connect(('192.0.2.1', 80))\""
        )
        self.assertIn("Command failed", output)

    async def test_read_tracking_and_stale_approval(self):
        inp = {"file_path": "app.py", "content": "value = 2\n"}
        with self.assertRaisesRegex(ValueError, "Read this file"):
            await self.workspace.execute("write_file", inp, self.state)
        await self.workspace.execute("read_file", {"file_path": "app.py"}, self.state)
        proposal = await self.workspace.prepare("write_file", inp)
        await self.shell("printf 'value = 3\\n' > app.py")
        with self.assertRaisesRegex(ValueError, "Read this file"):
            await self.workspace.execute("write_file", inp, self.state, proposal)
        await self.workspace.execute("read_file", {"file_path": "app.py"}, self.state)
        with self.assertRaisesRegex(ValueError, "after approval"):
            await self.workspace.execute("write_file", inp, self.state, proposal)

    async def test_timeout_destroys_container_and_refuses_further_calls(self):
        with self.assertRaisesRegex(RuntimeError, "timed out"):
            await self.shell("sleep 60 & wait", timeout=100)
        self.assertFalse(self.workspace.active)
        with self.assertRaises(RuntimeError):
            await self.workspace._docker("inspect", self.workspace.name)
        with self.assertRaises(RuntimeError):
            await self.shell("echo should-not-run")

    async def make_agent(self, mode="default"):
        agent = Agent(
            api_base="https://invalid.test/v1",
            api_key="test-only",
            model="test",
            permission_mode=mode,
            workspace=self.workspace,
            audit_path=str(self.workspace.output / "audit.jsonl"),
            confirm_fn=AsyncMock(return_value=False),
        )
        self.addAsyncCleanup(agent._openai_client.close)
        return agent

    async def test_real_agent_denial_and_no_host_tool_fallback(self):
        agent = await self.make_agent()
        with patch(
            "agents.agent.execute_tool", new=AsyncMock(side_effect=AssertionError("Host tool called"))
        ):
            await agent._execute_tool_call("read_file", {"file_path": "app.py"})
            result = await agent._execute_tool_call("write_file", {"file_path": "app.py", "content": "bad"})
            self.assertIn("Action denied", result)
            for name in ["skill_create", "skill", "mcp__external__tool"]:
                self.assertIn("Action denied", await agent._execute_tool_call(name, {}))
            self.assertIn("Entered plan mode", await agent._execute_tool_call("enter_plan_mode", {}))
            self.assertEqual(agent.permission_mode, "plan")
        text = await self.workspace.execute("read_file", {"file_path": "app.py"}, {})
        self.assertIn("value = 1", text)
        events = [
            json.loads(line) for line in (self.workspace.output / "audit.jsonl").read_text().splitlines()
        ]
        self.assertTrue(any(e.get("approved") is False for e in events))

    async def test_agent_loop_and_child_share_workspace_without_model_api(self):
        agent = await self.make_agent("acceptEdits")

        def response(name=None, inp=None):
            message = {"role": "assistant", "content": "done" if name is None else None}
            if name:
                message["tool_calls"] = [
                    {"id": name, "type": "function", "function": {"name": name, "arguments": json.dumps(inp)}}
                ]
            return {"choices": [{"message": message}]}

        with patch.object(
            agent,
            "_call_openai_stream",
            new=AsyncMock(
                side_effect=[
                    response("read_file", {"file_path": "app.py"}),
                    response("edit_file", {"file_path": "app.py", "old_string": "1", "new_string": "2"}),
                    response(),
                ]
            ),
        ):
            with patch.object(agent._mcp_manager, "load_and_connect", new=AsyncMock()) as mcp:
                await agent.run_once("Change the value to two")
                mcp.assert_not_called()
        self.assertEqual((self.source / "app.py").read_text(), "value = 1\n")

        async def run_child(child, prompt):
            try:
                self.assertIs(child.workspace, self.workspace)
                self.assertNotIn("run_shell", {t["name"] for t in child.tools})
                text = await child._execute_tool_call("read_file", {"file_path": "app.py"})
                self.assertIn("value = 2", text)
                return {"text": "child saw change", "tokens": {"input": 0, "cached": 0, "output": 0}}
            finally:
                await child._openai_client.close()

        with patch.object(Agent, "run_once", run_child):
            result = await agent._execute_tool_call("agent", {"type": "explore", "prompt": "Inspect"})
        self.assertEqual(json.loads(result)["text"], "child saw change")

    async def test_binary_delete_mode_export(self):
        await self.shell(
            "python -c \"from pathlib import Path; Path('app.py').unlink(); Path('binary.dat').write_bytes(bytes([0,255,1])); Path('exec.sh').write_text('echo ok\\n'); Path('exec.sh').chmod(0o755)\""
        )
        exported = await self.workspace.export_changes()
        self.assertEqual(exported["deleted"], ["app.py"])
        target = self.root / "apply"
        shutil.copytree(self.source, target)
        subprocess.run(
            ["git", "apply", "-p2", str(self.workspace.output / "changes.patch")],
            cwd=target,
            check=True,
            capture_output=True,
        )
        self.assertFalse((target / "app.py").exists())
        self.assertEqual((target / "binary.dat").read_bytes(), bytes([0, 255, 1]))
        self.assertTrue((target / "exec.sh").stat().st_mode & 0o111)

    async def test_cli_task_exports_result_and_cleans_up(self):
        from types import SimpleNamespace
        from agents.sandbox_cli import run_task

        skill = self.source / ".miniagent/skills/check"
        (skill / "scripts").mkdir(parents=True)
        (skill / "SKILL.md").write_text(
            "---\nallowed-tools: read_file, edit_file, run_shell\n---\n"
            "Run ${CLAUDE_SKILL_DIR}/scripts/check.py"
        )
        (skill / "scripts/check.py").write_text('print("SKILL_RESOURCE_OK")')
        args = SimpleNamespace(
            skill=["check"],
            no_memory=True,
            workspace=str(self.source),
            output_dir=str(self.root / "cli-results"),
            sandbox_image="miniagent-sandbox:1",
            task_timeout=30,
            model="test",
            thinking=False,
            max_cost=None,
            max_turns=10,
        )

        async def fake_model(agent):
            assert "/workspace/.task-skills/project/check/scripts/check.py" in str(agent._openai_messages)
            self.assertEqual({t["name"] for t in agent.tools}, {"read_file", "edit_file", "run_shell"})
            denied = await agent._execute_tool_call("write_file", {"file_path": "app.py", "content": "BAD"})
            self.assertIn("denied", denied.lower())
            calls = [
                (
                    "run_shell",
                    {
                        "command": "python /workspace/.task-skills/project/check/scripts/check.py && "
                        "echo temporary > /workspace/.task-skills/project/check/scratch.txt"
                    },
                ),
                ("read_file", {"file_path": "app.py"}),
                ("edit_file", {"file_path": "app.py", "old_string": "1", "new_string": "2"}),
                ("run_shell", {"command": 'python -c "from app import value; assert value == 2"'}),
            ]
            count = sum(m.get("role") == "assistant" for m in agent._openai_messages)
            message = {"role": "assistant", "content": "Updated and tested."}
            if count < len(calls):
                name, inp = calls[count]
                message["tool_calls"] = [
                    {
                        "id": str(count),
                        "type": "function",
                        "function": {"name": name, "arguments": json.dumps(inp)},
                    }
                ]
            return {"choices": [{"message": message}]}

        with patch.object(Agent, "_call_openai_stream", fake_model):
            await run_task(
                args,
                "Change value to two and test it",
                "https://invalid.test/v1",
                "test-only",
                True,
                "bypassPermissions",
            )
        reports = list((self.root / "cli-results").glob("*/result.json"))
        self.assertEqual(len(reports), 1)
        report = json.loads(reports[0].read_text())
        self.assertEqual(report["status"], "completed")
        self.assertEqual(report["context"]["skills"], ["project:check"])
        self.assertIn("SKILL_RESOURCE_OK", str(report["messages"]))
        self.assertEqual((skill / "scripts/check.py").read_text(), 'print("SKILL_RESOURCE_OK")')
        self.assertEqual(report["changes"]["changed"], ["app.py"])
        self.assertEqual((self.source / "app.py").read_text(), "value = 1\n")
        with self.assertRaises(RuntimeError):
            await self.workspace._docker("inspect", report["container"])

    async def test_context7_cli_keeps_execution_isolated(self):
        from types import SimpleNamespace
        import httpx
        from agents.context7 import Context7, PREFIX
        from agents.sandbox_cli import run_task
        from tests.test_context7 import Server

        server = Server()
        service = Context7(transport=httpx.MockTransport(server))
        args = SimpleNamespace(
            workspace=str(self.source),
            output_dir=str(self.root / "context7-results"),
            sandbox_image="miniagent-sandbox:1",
            task_timeout=30,
            model="test",
            thinking=False,
            max_cost=None,
            max_turns=10,
            context7=True,
            skill=[],
            no_memory=True,
        )

        async def fake_model(agent):
            calls = [
                (PREFIX + "query-docs", {"libraryId": "/pytest-dev/pytest", "query": "How to use fixtures?"}),
                ("mcp__playwright__browser_eval", {"code": "bad"}),
                ("write_file", {"file_path": str(self.root / "sentinel"), "content": "BAD"}),
                ("read_file", {"file_path": "app.py"}),
            ]
            count = sum(m.get("role") == "assistant" for m in agent._openai_messages)
            message = {"role": "assistant", "content": "Checked documentation."}
            if count < len(calls):
                name, inp = calls[count]
                message["tool_calls"] = [
                    {
                        "id": str(count),
                        "type": "function",
                        "function": {"name": name, "arguments": json.dumps(inp)},
                    }
                ]
            return {"choices": [{"message": message}]}

        with (
            patch("agents.sandbox_cli.Context7", return_value=service),
            patch.object(Agent, "_call_openai_stream", fake_model),
            patch("agents.agent.execute_tool", new=AsyncMock(side_effect=AssertionError("Host execution"))),
        ):
            await run_task(
                args,
                "Check public documentation",
                "https://invalid.test/v1",
                "test-only",
                True,
                "bypassPermissions",
            )
        report = json.loads(next((self.root / "context7-results").glob("*/result.json")).read_text())
        self.assertTrue(report["context7_enabled"])
        self.assertIn("Untrusted external documentation", str(report["messages"]))
        self.assertIn("Action denied", str(report["messages"]))
        self.assertEqual(report["changes"]["changed"], [])
        self.assertEqual((self.root / "sentinel").read_text(), "KEEP_ME")
        self.assertTrue(service.connection._client.is_closed)
        self.assertEqual(len(server.requests), 5)  # Handshake, discovery, call, and session close.

    async def test_stuck_cli_preserves_patch_output_and_stopped_status(self):
        from types import SimpleNamespace
        from agents.sandbox_cli import run_task

        args = SimpleNamespace(
            workspace=str(self.source),
            output_dir=str(self.root / "stuck-results"),
            sandbox_image="miniagent-sandbox:1",
            task_timeout=30,
            model="test",
            thinking=False,
            max_cost=None,
            max_turns=20,
            skill=[],
            no_memory=True,
        )

        async def fake_model(agent):
            count = sum(m.get("role") == "assistant" for m in agent._openai_messages)
            if count > 8:
                self.fail("Loop did not stop")
            calls = [
                ("read_file", {"file_path": "app.py"}),
                ("edit_file", {"file_path": "app.py", "old_string": "1", "new_string": "2"}),
                ("run_shell", {"command": 'python -c "print("TEST_OUTPUT_PRESERVED")"'}),
            ]
            name, inp = calls[count] if count < len(calls) else ("read_file", {"file_path": "app.py"})
            return {
                "choices": [
                    {
                        "message": {
                            "role": "assistant",
                            "content": None,
                            "tool_calls": [
                                {
                                    "id": str(count),
                                    "type": "function",
                                    "function": {"name": name, "arguments": json.dumps(inp)},
                                }
                            ],
                        }
                    }
                ]
            }

        with (
            patch.object(Agent, "_call_openai_stream", fake_model),
            self.assertRaisesRegex(RuntimeError, "Task stopped"),
        ):
            await run_task(
                args, "Inspect and test", "https://invalid.test/v1", "test-only", True, "bypassPermissions"
            )
        folder = next((self.root / "stuck-results").iterdir())
        report = json.loads((folder / "result.json").read_text())
        self.assertEqual(report["status"], "stopped")
        self.assertIn("repeated_result", report["stop_reason"])
        self.assertIn("TEST_OUTPUT_PRESERVED", str(report["messages"]))
        self.assertIn("value = 2", (folder / "changes.patch").read_text())
        self.assertEqual((self.source / "app.py").read_text(), "value = 1\n")
        with self.assertRaises(RuntimeError):
            await self.workspace._docker("inspect", report["container"])

    async def test_parent_reports_sandbox_unavailable(self):
        agent = await self.make_agent()
        await self.workspace.close()
        response = {"choices": [{"message": {
            "role": "assistant",
            "content": None,
            "tool_calls": [{
                "id": "read",
                "type": "function",
                "function": {"name": "read_file", "arguments": '{"file_path": "app.py"}'},
            }],
        }}]}
        with patch.object(agent, "_call_openai_stream", new=AsyncMock(return_value=response)) as model:
            result = await agent.run_once("Inspect app.py")
        model.assert_awaited_once()
        self.assertTrue(agent._aborted)
        self.assertEqual(result["status"], "stopped")
        self.assertEqual(result["stop_reason"], "Sandbox unavailable")
        self.assertIn("Sandbox unavailable", str(agent._openai_messages))
        self.assertEqual((self.source / "app.py").read_text(), "value = 1\n")

    async def test_cli_reports_sandbox_unavailable_after_child_stops(self):
        from types import SimpleNamespace

        from agents.sandbox_cli import run_task

        args = SimpleNamespace(
            workspace=str(self.source),
            output_dir=str(self.root / "unavailable-results"),
            sandbox_image="miniagent-sandbox:1",
            task_timeout=30,
            model="test",
            thinking=False,
            max_cost=None,
            max_turns=10,
            skill=[],
            no_memory=True,
        )
        results = {}
        original_run_once = Agent.run_once

        async def record_result(agent, prompt):
            result = await original_run_once(agent, prompt)
            results["child" if agent.is_sub_agent else "parent"] = result
            return result

        async def fake_model(agent):
            if agent.is_sub_agent:
                # Destroy the shared container while only the child is running.
                await agent.workspace.close()
                name, inp = "read_file", {"file_path": "app.py"}
            elif any(m.get("role") == "assistant" for m in agent._openai_messages):
                self.assertFalse(agent._aborted)
                return {"choices": [{"message": {
                    "role": "assistant", "content": "Child could not inspect the workspace."
                }}]}
            else:
                name, inp = "agent", {"type": "explore", "prompt": "Inspect app.py"}
            return {"choices": [{"message": {
                "role": "assistant",
                "content": None,
                "tool_calls": [{
                    "id": name,
                    "type": "function",
                    "function": {"name": name, "arguments": json.dumps(inp)},
                }],
            }}]}

        with (
            patch.object(Agent, "_call_openai_stream", fake_model),
            patch.object(Agent, "run_once", record_result),
            patch.object(DockerWorkspace, "export_changes", new=AsyncMock()) as export,
            self.assertRaisesRegex(RuntimeError, "Task stopped"),
        ):
            await run_task(
                args, "Delegate inspection", "https://invalid.test/v1",
                "test-only", True, "bypassPermissions",
            )
        export.assert_not_awaited()

        self.assertEqual(results["child"]["status"], "stopped")
        self.assertEqual(results["child"]["stop_reason"], "Sandbox unavailable")
        self.assertEqual(results["parent"]["status"], "completed")
        self.assertIsNone(results["parent"]["stop_reason"])
        folder = next((self.root / "unavailable-results").iterdir())
        report = json.loads((folder / "result.json").read_text())
        self.assertEqual(report["status"], "stopped")
        self.assertEqual(report["stop_reason"], "Sandbox unavailable")
        self.assertIn("Child could not inspect", str(report["messages"]))
        self.assertNotIn("changes", report)
        self.assertFalse((folder / "changes.patch").exists())
        self.assertEqual((self.source / "app.py").read_text(), "value = 1\n")
        with self.assertRaises(RuntimeError):
            await self.workspace._docker("inspect", report["container"])

    async def test_plan_approval_keeps_edits_inside_container_and_requires_confirmation(self):
        agent = await self.make_agent("plan")
        plan = "Change value to two, then verify the result."
        decisions = AsyncMock(side_effect=[
            {"choice": "keep-planning", "feedback": "Read the file first"},
            {"choice": "execute"},
        ])
        agent.set_plan_approval_fn(decisions)
        result = await agent._execute_tool_call("write_file", {"file_path": "app.py", "content": "bad"})
        self.assertEqual(result.status, "denied")
        result = await agent._execute_tool_call("exit_plan_mode", {"plan": plan})
        self.assertIn("Read the file first", result)
        self.assertEqual(agent.permission_mode, "plan")
        await agent._execute_tool_call("exit_plan_mode", {"plan": plan})
        self.assertEqual(agent.permission_mode, "default")
        decisions.assert_awaited_with(plan)
        self.assertEqual((self.workspace.output / "plan.md").read_text(), plan)
        await agent._execute_tool_call("read_file", {"file_path": "app.py"})
        edit = {"file_path": "app.py", "old_string": "1", "new_string": "2"}
        with patch("agents.agent.execute_tool", new=AsyncMock(side_effect=AssertionError("Host execution"))):
            result = await agent._execute_tool_call("edit_file", edit)
            self.assertEqual(result.status, "denied")
            agent.confirm_fn.assert_awaited_once()
            agent.confirm_fn.return_value = True
            result = await agent._execute_tool_call("edit_file", edit)
            self.assertEqual(result.status, "success")
            self.assertEqual(agent.confirm_fn.await_count, 2)
            await agent._execute_tool_call("read_file", {"file_path": "app.py"})
            await agent._execute_tool_call("edit_file", {**edit, "old_string": "2", "new_string": "3"})
            self.assertEqual(agent.confirm_fn.await_count, 3)
        text = await self.workspace.execute("read_file", {"file_path": "app.py"}, {})
        self.assertIn("value = 3", text)
        self.assertEqual((self.source / "app.py").read_text(), "value = 1\n")

    async def test_cancelled_execution_destroys_container(self):
        import asyncio

        task = asyncio.create_task(self.shell("sleep 60"))
        await asyncio.sleep(0.2)
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task
        self.assertFalse(self.workspace.active)
        with self.assertRaises(RuntimeError):
            await self.workspace._docker("inspect", self.workspace.name)

    async def test_audit_failure_cannot_execute_edit(self):
        agent = await self.make_agent("acceptEdits")
        await agent._execute_tool_call("read_file", {"file_path": "app.py"})
        with patch.object(agent, "_audit_permission", side_effect=OSError("audit unavailable")):
            with self.assertRaises(OSError):
                await agent._execute_tool_call("write_file", {"file_path": "app.py", "content": "bad"})
        result = await self.workspace.execute("read_file", {"file_path": "app.py"}, {})
        self.assertIn("value = 1", result)
