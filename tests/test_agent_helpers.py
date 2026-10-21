import unittest
from unittest.mock import patch

from agents.agent import Agent


class SkillQueryTests(unittest.TestCase):
    def test_explicit_query_does_not_replace_task_payload(self):
        agent = Agent.__new__(Agent)
        hit = {"all_hits": [{"name": "example_skill"}]}
        with patch(
            "agents.skills.format_retrieved_skill_context", return_value=("CHECKLIST", hit)
        ) as retrieve:
            text, reference = agent._augment_user_message_with_skill_context(
                "large log payload", query="Diagnose service"
            )
        retrieve.assert_called_once_with("Diagnose service", limit=3)
        self.assertIn("large log payload", text)
        self.assertIn("CHECKLIST", text)
        self.assertIs(reference, hit)


class ThinkingModeTests(unittest.IsolatedAsyncioTestCase):
    async def test_stream_request_uses_resolved_thinking_mode(self):
        from types import SimpleNamespace
        from unittest.mock import AsyncMock, MagicMock

        cases = [
            ("claude-sonnet-4-6", True, {"type": "adaptive"}),
            ("claude-opus-4-6", True, {"type": "adaptive"}),
            ("claude-sonnet-4-5", True, {"type": "enabled"}),
            ("claude-sonnet-4-6", False, None),
            ("claude-3-5-sonnet", True, None),
        ]
        for model, thinking, expected in cases:
            with self.subTest(model=model, thinking=thinking):
                agent = Agent(
                    model=model, api_key="test", thinking=thinking,
                    is_sub_agent=True, custom_system_prompt="Test",
                )
                self.addAsyncCleanup(agent._anthropic_client.close)
                agent._anthropic_messages = [{"role": "user", "content": "Hello"}]
                final = SimpleNamespace(content=[])
                stream = MagicMock()
                stream.__aiter__.return_value = []
                stream.get_final_message = AsyncMock(return_value=final)
                manager = MagicMock()
                manager.__aenter__ = AsyncMock(return_value=stream)
                manager.__aexit__ = AsyncMock(return_value=False)
                with patch.object(
                    agent._anthropic_client.messages, "stream", return_value=manager
                ) as request:
                    self.assertIs(await agent._call_anthropic_stream(), final)
                request.assert_called_once()
                params = request.call_args.kwargs
                self.assertEqual(params["model"], model)
                self.assertEqual(params["messages"], agent._anthropic_messages)
                if expected is None:
                    self.assertNotIn("thinking", params)
                elif expected["type"] == "adaptive":
                    self.assertEqual(params["thinking"], expected)
                else:
                    self.assertEqual(
                        params["thinking"],
                        {"type": "enabled", "budget_tokens": params["max_tokens"] - 1},
                    )
                    self.assertGreaterEqual(params["thinking"]["budget_tokens"], 1024)
                stream.get_final_message.assert_awaited_once()

    async def test_construction_resolves_thinking_mode(self):
        cases = [
            ("claude-sonnet-4-6", True, "adaptive"),
            ("claude-sonnet-4-5", True, "enabled"),
            ("claude-3-5-sonnet", True, "disabled"),
            ("deepseek-chat", True, "disabled"),
            ("claude-sonnet-4-6", False, "disabled"),
        ]
        for model, thinking, expected in cases:
            with self.subTest(model=model, thinking=thinking):
                agent = Agent(model=model, api_key="test", thinking=thinking,
                              is_sub_agent=True, custom_system_prompt="Test")
                self.addAsyncCleanup(agent._anthropic_client.close)
                self.assertEqual(agent._thinking_mode, expected)


class RequestCountTests(unittest.IsolatedAsyncioTestCase):
    async def test_text_and_failed_dispatches_count_without_changing_tools(self):
        agent = Agent.__new__(Agent)
        agent.tools = [{"name": "read_file"}]
        original = agent.tools

        async def text_reply(**kwargs):
            self.assertEqual(kwargs, {"value": 3})
            return {"text": "result"}

        self.assertEqual(await agent._dispatch_model_request(text_reply, value=3), {"text": "result"})

        async def failed():
            raise RuntimeError("failed request")

        with self.assertRaises(RuntimeError):
            await agent._dispatch_model_request(failed)
        self.assertEqual(agent.model_requests, 2)
        self.assertIs(agent.tools, original)


class StuckDetectorTests(unittest.TestCase):
    def test_canonical_parameters_warning_and_stop(self):
        from agents.stuck_detector import StuckDetector, ToolResult

        detector = StuckDetector()
        for count in range(1, 5):
            args = {"b": 2, "a": 1} if count % 2 else {"a": 1, "b": 2}
            event = detector.observe("read_file", args, ToolResult("same"), 0)
            self.assertEqual(event["action"] if event else None, {3: "warn", 4: "stop"}.get(count))

    def test_different_files_results_and_actual_changes_do_not_stop(self):
        from agents.stuck_detector import StuckDetector, ToolResult

        for changing in ["path", "result", "version"]:
            detector = StuckDetector()
            for i in range(8):
                event = detector.observe(
                    "read_file",
                    {"path": str(i) if changing == "path" else "a"},
                    ToolResult("same-prefix" + (str(i) if changing == "result" else "")),
                    i if changing == "version" else 0,
                )
                self.assertIsNone(event)

    def test_noop_writes_and_compaction_do_not_reset(self):
        from agents.stuck_detector import StuckDetector, ToolResult

        detector = StuckDetector()
        for _ in range(4):
            event = detector.observe("run_shell", {"command": "pytest"}, ToolResult("FAILED"), 0)
            detector.observe("write_file", {"file_path": "a"}, ToolResult("written", changed=False), 0)
            detector.observe("compact_context", {}, ToolResult("compacted"), 0)
        self.assertEqual(event["action"], "stop")

    def test_changing_error_text_and_denial_totals(self):
        from agents.stuck_detector import StuckDetector, ToolResult

        detector = StuckDetector()
        for i in range(4):
            event = detector.observe("run_shell", {"command": "pytest"}, ToolResult(str(i), "error"), 0)
        self.assertEqual(event["pattern"], "repeated_error")
        detector = StuckDetector()
        for i in range(8):
            detector.observe("read_file", {"path": str(i)}, ToolResult("ok"), i)
            event = detector.observe(
                "run_shell", {"command": "blocked " + str(i)}, ToolResult("no", "denied"), i
            )
        self.assertEqual(event["pattern"], "tool_denial_total")
        self.assertEqual(event["action"], "stop")

    def test_denied_compaction_counts_as_denial(self):
        from agents.stuck_detector import StuckDetector, ToolResult

        detector = StuckDetector()
        for i in range(1, 9):
            event = detector.observe("compact_context", {"reason": str(i)}, ToolResult("no", "denied"), 0)
            self.assertEqual(event["action"] if event else None, {6: "warn", 8: "stop"}.get(i))
        self.assertEqual(event["pattern"], "tool_denial_total")
        detector = StuckDetector()
        for _ in range(4):
            event = detector.observe("compact_context", {}, ToolResult("no", "denied"), 0)
        self.assertEqual((event["pattern"], event["action"]), ("repeated_denial", "stop"))

    def test_child_stops_count_per_tool_across_prompts(self):
        from agents.stuck_detector import StuckDetector, ToolResult

        detector = StuckDetector()
        for i in range(1, 5):
            detector.observe("agent", {"prompt": "done " + str(i)}, ToolResult("finished"), i)
            event = detector.observe("agent", {"prompt": str(i)}, ToolResult(str(i), "stopped"), i)
            self.assertEqual(event["action"] if event else None, {3: "warn", 4: "stop"}.get(i))
        self.assertEqual(event["pattern"], "repeated_child_stop")

    def test_child_stop_breaks_consecutive_error_streak(self):
        from agents.stuck_detector import StuckDetector, ToolResult

        detector = StuckDetector()
        for _ in range(3):
            detector.observe("read_file", {"file_path": "x"}, ToolResult("missing", "error"), 0)
        detector.observe("agent", {"prompt": "try"}, ToolResult("partial", "stopped"), 0)
        self.assertIsNone(detector.observe("read_file", {"file_path": "x"}, ToolResult("missing", "error"), 0))
        self.assertEqual(detector.errors, 1)
        self.assertEqual(detector.child_stops["agent"], 1)

    def test_shell_spacing_and_explicit_approval(self):
        from agents.stuck_detector import StuckDetector, ToolResult

        detector = StuckDetector()
        self.assertNotEqual(
            detector.call_key("run_shell", {"command": 'echo "a  b"'}),
            detector.call_key("run_shell", {"command": 'echo "a b"'}),
        )
        for i in range(3):
            event = detector.observe(
                "run_shell", {"command": "rm" + " " * (i + 1) + "file"}, ToolResult("no", "denied"), 0
            )
        self.assertEqual(event["action"], "warn")
        detector.observe("run_shell", {"command": "rm file"}, ToolResult("ok", approved=True), 0)
        self.assertIsNone(
            detector.observe("run_shell", {"command": "rm file"}, ToolResult("no", "denied"), 0)
        )

    def test_duration_normalization_retains_other_numbers_and_full_tail(self):
        from agents.stuck_detector import StuckDetector

        key = StuckDetector.result_key
        self.assertEqual(
            key("run_shell", "=== 1 passed in 0.12s ==="), key("run_shell", "=== 1 passed in 3.45s ===")
        )
        self.assertNotEqual(
            key("run_shell", "=== 1 passed in 0.12s ==="), key("run_shell", "=== 2 passed in 0.12s ===")
        )
        self.assertNotEqual(key("read_file", "x" * 100000 + "a"), key("read_file", "x" * 100000 + "b"))


class StuckLoopTests(unittest.IsolatedAsyncioTestCase):
    async def run_loop(self, backend, *, batch=False):
        import asyncio
        import json
        import tempfile
        from pathlib import Path
        from types import SimpleNamespace as NS
        from unittest.mock import AsyncMock
        from agents.stuck_detector import ToolResult

        with tempfile.TemporaryDirectory() as directory:
            kwargs = {"api_base": "https://invalid.test/v1"} if backend == "openai" else {}
            agent = Agent(
                **kwargs,
                api_key="test",
                model="test",
                is_sub_agent=True,
                custom_system_prompt="Test",
                permission_mode="bypassPermissions",
                audit_path=str(Path(directory) / "audit.jsonl"),
                max_turns=20,
            )
            self.addAsyncCleanup((agent._openai_client or agent._anthropic_client).close)
            agent._check_and_compact = AsyncMock()
            dispatched = []

            async def execute(name, inp):
                dispatched.append(name)
                await asyncio.sleep(0)
                return ToolResult("same output")

            agent._execute_tool_impl = execute
            count = 0

            async def response(**kwargs):
                nonlocal count
                count += 1
                if count > 5:
                    self.fail("Agent did not stop")
                names = ["read_file"] * 4 + ["write_file"] if batch else ["read_file"]
                calls = [
                    (str(count) + "-" + str(i), name, {"file_path": "a"}) for i, name in enumerate(names)
                ]
                if count == 4:
                    history = agent._openai_messages if backend == "openai" else agent._anthropic_messages
                    self.assertIn("Change approach", str(history))
                if backend == "openai":
                    return {
                        "choices": [
                            {
                                "message": {
                                    "role": "assistant",
                                    "content": None,
                                    "tool_calls": [
                                        {
                                            "id": ident,
                                            "type": "function",
                                            "function": {"name": name, "arguments": json.dumps(inp)},
                                        }
                                        for ident, name, inp in calls
                                    ],
                                }
                            }
                        ]
                    }
                if callback := kwargs.get("on_tool_block_complete"):
                    for ident, name, inp in calls:
                        callback({"id": ident, "name": name, "input": inp})
                return NS(
                    content=[
                        NS(type="tool_use", id=ident, name=name, input=inp) for ident, name, inp in calls
                    ],
                    usage=NS(input_tokens=1, output_tokens=1),
                )

            agent._call_openai_stream = response
            agent._call_anthropic_stream = response
            result = await agent.run_once("Inspect")
            self.assertEqual(result["status"], "stopped")
            self.assertIn("repeated_result", result["stop_reason"])
            self.assertEqual(count, 1 if batch else 4)
            self.assertEqual(dispatched, ["read_file"] * 4)
            events = [json.loads(line) for line in (Path(directory) / "audit.jsonl").read_text().splitlines()]
            detection = [event for event in events if event["phase"] == "stuck_detection"]
            self.assertEqual([e["action"] for e in detection], ["warn", "stop"])
            self.assertFalse(any("hash" in key or "arguments" in key for e in detection for key in e))
            history = agent._openai_messages if backend == "openai" else agent._anthropic_messages
            self.assertIn("Change approach", str(history))
            if batch:
                self.assertIn("Tool skipped: task stopped", str(history))
            self.assertEqual(agent.tool_stats()["calls"], 4)

    async def test_both_model_interfaces_stop_after_warning(self):
        for backend in ["openai", "anthropic"]:
            await self.run_loop(backend)

    async def test_concurrent_and_early_streaming_calls_are_collected_in_order(self):
        for backend in ["openai", "anthropic"]:
            await self.run_loop(backend, batch=True)
