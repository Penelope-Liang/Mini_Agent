"""Default CLI: one disposable workspace and fresh Agent per submitted task."""

from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path
import time

from .agent import Agent
from .context7 import Context7, tool_definitions as context7_tools
from .sandbox_context import load_context
from .terminal_approval import TerminalApproval
from .tools import tool_definitions
from .ui import print_error, print_info
from .workspace import DockerWorkspace


async def run_task(args, prompt, api_base, api_key, use_openai, permission_mode):
    workspace = DockerWorkspace(Path(args.workspace), Path(args.output_dir), args.sandbox_image)
    workspace.context7 = None
    agent = None
    approval = TerminalApproval()
    deadline = asyncio.timeout(args.task_timeout)
    started = time.monotonic()
    status = {
        "task": prompt,
        "status": "failed",
        "source": str(workspace.source),
        "container": workspace.name,
        "image": workspace.image,
    }
    try:
        async with deadline:
            context = await asyncio.to_thread(
                load_context,
                workspace.source,
                getattr(args, "skill", []),
                prompt,
                memory=not getattr(args, "no_memory", False),
            )
            status["context"] = {k: context[k] for k in ("skills", "memories")}
            await workspace.start(context["files"])
            print_info(f"Isolated workspace: {workspace.name}; results: {workspace.output}")
            tools = list(tool_definitions)
            if getattr(args, "context7", False):
                secrets = [api_key] + [
                    value
                    for key, value in os.environ.items()
                    if any(word in key.upper() for word in ("KEY", "TOKEN", "SECRET", "PASSWORD"))
                ]
                workspace.context7 = Context7(os.environ.get("CONTEXT7_API_KEY", ""), secrets=secrets)
                tools += context7_tools()
            status["context7_enabled"] = workspace.context7 is not None
            agent = Agent(
                custom_tools=tools
                if context["allowed_tools"] is None
                else [tool for tool in tools if tool["name"] in context["allowed_tools"]],
                model=args.model,
                api_base=api_base if use_openai else None,
                anthropic_base_url=api_base if not use_openai else None,
                api_key=api_key,
                permission_mode=permission_mode,
                thinking=args.thinking,
                max_cost_usd=args.max_cost,
                max_turns=args.max_turns,
                workspace=workspace,
                confirm_fn=approval,
                audit_path=str(workspace.output / "audit.jsonl"),
            )
            agent.set_plan_approval_fn(approval.plan)
            result = await agent.run_once(prompt + context["text"])
            status.update(result)
            status["status"] = (
                "stopped" if result.get("status") == "stopped" or not workspace.active else "completed"
            )
            if not workspace.active and not status.get("stop_reason"):
                status["stop_reason"] = "Sandbox unavailable"
            if workspace.active:
                status["changes"] = await workspace.export_changes()
                print_info(f"Changes exported to {workspace.output}; original files were not modified.")
            if status["status"] != "completed":
                raise RuntimeError("Task stopped before completion; inspect result.json")
    except BaseException as exc:
        if deadline.expired():
            status.update(
                status="stopped",
                stop_reason="Task timed out while waiting for approval"
                if approval.cancelled_while_waiting else "Task timed out",
            )
        elif isinstance(exc, (asyncio.CancelledError, KeyboardInterrupt)):
            status.update(
                status="stopped",
                stop_reason="Task interrupted while waiting for approval"
                if approval.cancelled_while_waiting else "Task interrupted",
            )
        status.update(
            status="stopped" if status["status"] == "stopped" else "failed",
            error=f"{type(exc).__name__}: {exc}",
        )
        raise
    finally:
        await workspace.close()
        if workspace.context7 is not None:
            await workspace.context7.close()
        if agent:
            if agent.plan_content is not None:
                status["plan"] = agent.plan_content
            client = agent._openai_client or agent._anthropic_client
            await client.close()
            status["tokens"] = agent.get_token_usage()
            status["estimated_cost_usd"] = agent._get_current_cost_usd()
            status["messages"] = agent._openai_messages if use_openai else agent._anthropic_messages
        status["seconds"] = time.monotonic() - started
        if workspace.output.exists():
            (workspace.output / "result.json").write_text(json.dumps(status, ensure_ascii=False, indent=2))


def run_cli(args, api_base, api_key, use_openai, permission_mode):
    if args.resume:
        raise ValueError("--resume is unavailable: each sandbox task starts from a fresh source copy")
    if args.task_timeout <= 0:
        raise ValueError("--task-timeout must be positive")
    prompt = " ".join(args.prompt)
    if prompt:
        asyncio.run(run_task(args, prompt, api_base, api_key, use_openai, permission_mode))
        return
    print_info("Sandbox mode: each message starts a fresh task. Enter exit to quit.")
    while True:
        try:
            prompt = input("Task> ").strip()
        except (EOFError, KeyboardInterrupt):
            break
        if prompt in {"exit", "quit"}:
            break
        if not prompt:
            continue
        if prompt.startswith("/"):
            print_error("REPL slash commands are unavailable in isolated task mode; use CLI flags.")
            continue
        try:
            asyncio.run(run_task(args, prompt, api_base, api_key, use_openai, permission_mode))
        except (Exception, KeyboardInterrupt) as exc:
            print_error(str(exc) or "Task interrupted")
