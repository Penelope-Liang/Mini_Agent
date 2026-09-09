"""
MCP client module.

Connects to stdio-based MCP servers, discovers tools, and forwards Agent tool calls
to the corresponding MCP server.

Implementation:
- No MCP SDK dependency; JSON-RPC over stdin/stdout.
- Each MCP server runs in its own subprocess.
- MCP tools are exposed as `mcp__serverName__toolName` to avoid local name collisions.

Config sources:
- Global: `~/.miniagent/settings.json`
- Project: `.miniagent/settings.json`
- Claude Code convention: `.mcp.json`

Example config:
{
    "mcpServers": {
        "name": {
            "command": "...",
            "args": [...],
            "env": {...}
        }
    }
}
"""

from __future__ import annotations

import asyncio
import json
import os
import subprocess
from pathlib import Path
from typing import Any

from .ui import print_error, print_info


# ─── Single MCP connection: one McpConnection per MCP server subprocess ──────────────────


class McpConnection:
    """Manage one MCP server subprocess and JSON-RPC communication with it."""

    def __init__(self, server_name: str, command: str, args: list[str] | None = None,
                 env: dict[str, str] | None = None):
        # Server name from config; used for tool-name prefixing and routing.
        self.server_name = server_name
        # Command used to start the MCP server, e.g. node, python, or a binary path.
        self.command = command
        # Arguments passed to the startup command.
        self.args = args or []
        # Extra env vars merged with the current process environment.
        self.env = env or {}
        # MCP server subprocess handle; None until connected.
        self._process: asyncio.subprocess.Process | None = None
        # Incrementing JSON-RPC request id counter.
        self._next_id = 1
        # Pending requests keyed by JSON-RPC id.
        self._pending: dict[int, asyncio.Future] = {}
        # Background task that reads MCP server stdout.
        self._reader_task: asyncio.Task | None = None

    async def connect(self) -> None:
        """Start the MCP server subprocess and begin reading stdout in the background."""
        # Child env = current process env + configured extras.
        merged_env = {**os.environ, **self.env}
        # Start MCP server in stdio mode:
        # - stdin: client writes JSON-RPC requests.
        # - stdout: server returns JSON-RPC responses.
        # - stderr: keep a separate error stream.
        self._process = await asyncio.create_subprocess_exec(
            self.command, *self.args,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env=merged_env,
        )
        # Read stdout in the background so connect() does not block initialization.
        self._reader_task = asyncio.create_task(self._read_loop())

    async def _read_loop(self) -> None:
        """Continuously read line-delimited JSON-RPC responses from MCP server stdout."""
        assert self._process and self._process.stdout
        while True:
            # MCP stdio typically uses one JSON-RPC message per line.
            line = await self._process.stdout.readline()
            if not line:
                # EOF on stdout usually means the subprocess exited.
                break
            try:
                msg = json.loads(line)
            except json.JSONDecodeError:
                # Ignore invalid JSON so one bad line does not break the connection.
                continue

            # JSON-RPC responses include id; notifications usually do not.
            msg_id = msg.get("id")
            if msg_id is not None and msg_id in self._pending:
                fut = self._pending.pop(msg_id)
                if "error" in msg:
                    # Mark the pending Future as failed on JSON-RPC error.
                    e = msg["error"]
                    fut.set_exception(
                        RuntimeError(f"MCP error {e.get('code')}: {e.get('message')}")
                    )
                else:
                    # Deliver only the result portion to the caller.
                    fut.set_result(msg.get("result"))

    async def _send_request(self, method: str, params: dict | None = None) -> Any:
        """Send a JSON-RPC request and await the matching response id."""
        assert self._process and self._process.stdin
        # Assign a unique request id looked up by the read loop.
        req_id = self._next_id
        self._next_id += 1

        # JSON-RPC 2.0 request format:
        # {
        #   "jsonrpc": "2.0",
        #   "id": 1,
        #   "method": "...",
        #   "params": {...}
        # }
        msg = json.dumps({"jsonrpc": "2.0", "id": req_id, "method": method, "params": params or {}})
        # stdio transport uses newline as the message boundary.
        self._process.stdin.write((msg + "\n").encode())
        await self._process.stdin.drain()

        # Register a Future in _pending until _read_loop receives the matching response.
        loop = asyncio.get_event_loop()
        fut: asyncio.Future = loop.create_future()
        self._pending[req_id] = fut
        return await fut

    def _send_notification(self, method: str, params: dict | None = None) -> None:
        """Send a JSON-RPC notification with no id and no response wait."""
        if not self._process or not self._process.stdin:
            return
        msg = json.dumps({"jsonrpc": "2.0", "method": method, "params": params or {}})
        self._process.stdin.write((msg + "\n").encode())

    async def initialize(self) -> None:
        """Run the MCP initialization handshake."""
        # initialize negotiates protocol version and client info after connect.
        await self._send_request("initialize", {
            "protocolVersion": "2024-11-05",
            "capabilities": {},
            "clientInfo": {"name": "miniagent", "version": "1.0.0"},
        })
        # After initialize succeeds, send the initialized notification.
        self._send_notification("notifications/initialized")

    async def list_tools(self) -> list[dict]:
        """List tools exposed by the current MCP server."""
        result = await self._send_request("tools/list")
        if not result or not isinstance(result.get("tools"), list):
            return []
        # Keep MCP inputSchema and attach serverName for prefixing and routing upstream.
        return [
            {
                "name": t["name"],
                "description": t.get("description", ""),
                "inputSchema": t.get("inputSchema"),
                "serverName": self.server_name,
            }
            for t in result["tools"]
        ]

    async def call_tool(self, name: str, args: dict) -> str:
        """Call a tool on the current MCP server and return a string result."""
        result = await self._send_request("tools/call", {"name": name, "arguments": args})
        if isinstance(result, dict) and isinstance(result.get("content"), list):
            # MCP tool results are usually content blocks; this Agent consumes text blocks only.
            return "\n".join(
                c["text"] for c in result["content"] if c.get("type") == "text"
            )
        # Fall back to JSON string if the result is not a standard content list.
        return json.dumps(result)

    def close(self) -> None:
        """Close the MCP server subprocess and fail all pending requests."""
        if self._reader_task:
            # Stop the stdout background reader.
            self._reader_task.cancel()
            self._reader_task = None
        if self._process:
            try:
                # Kill the subprocess so external MCP servers do not linger.
                self._process.kill()
            except ProcessLookupError:
                # Ignore ProcessLookupError if the process already exited.
                pass
            self._process = None
        # Fail any requests still pending after the connection closes.
        for fut in self._pending.values():
            if not fut.done():
                fut.set_exception(RuntimeError(f"MCP server '{self.server_name}' closed"))
        self._pending.clear()


# ─── MCP manager: manage multiple MCP server connections and tool routing ─────────────────────────────


class McpManager:
    """
    Manage all MCP server connections.

    Usage:
    1. Call load_and_connect() to read config, connect servers, and discover tools.
    2. Call get_tool_definitions() to expose MCP tools to the model.
    3. Route mcp__server__tool calls via call_tool().
    """

    def __init__(self):
        # Connected MCP servers keyed by server name.
        self._connections: dict[str, McpConnection] = {}
        # Tool definitions discovered from MCP servers.
        self._tools: list[dict] = []
        # Prevent duplicate connections; load_and_connect() should run once.
        self._connected = False

    async def load_and_connect(self) -> None:
        """Load config, connect all configured MCP servers, and discover their tools."""
        if self._connected:
            return
        self._connected = True

        # Merge global, project, and .mcp.json config; later entries override same names.
        configs = self._load_configs()
        if not configs:
            return

        # Allow up to 15 seconds per server init/tool discovery to avoid startup hangs.
        timeout = 15.0

        for name, cfg in configs.items():
            # Create a connection object from config.
            conn = McpConnection(
                name,
                cfg["command"],
                cfg.get("args"),
                cfg.get("env"),
            )
            try:
                # Connect subprocess -> initialize -> list tools.
                await conn.connect()
                await asyncio.wait_for(conn.initialize(), timeout=timeout)
                server_tools = await asyncio.wait_for(conn.list_tools(), timeout=timeout)
                # Register the connection only if init and tool discovery succeed.
                self._connections[name] = conn
                self._tools.extend(server_tools)
                print_info(f"MCP connected: {name} ({len(server_tools)} tools)")
            except Exception as e:
                # One failed server must not block others; close failed connections.
                print_error(f"MCP failed to connect: {name}: {e}")
                conn.close()

    def get_tool_definitions(self) -> list[dict]:
        """Return Agent/Anthropic tool definitions with prefixed MCP tool names."""
        return [
            {
                # Prefix format: mcp__serverName__toolName to avoid collisions and enable routing.
                "name": f"mcp__{t['serverName']}__{t['name']}",
                "description": t.get("description") or f"MCP tool {t['name']} from {t['serverName']}",
                # Anthropic uses input_schema; MCP tools usually expose inputSchema.
                "input_schema": t.get("inputSchema") or {"type": "object", "properties": {}},
            }
            for t in self._tools
        ]

    def is_mcp_tool(self, name: str) -> bool:
        """Return whether a tool name is an MCP-prefixed tool."""
        return name.startswith("mcp__")

    async def call_tool(self, prefixed_name: str, args: dict) -> str:
        """Route a prefixed MCP tool call to the correct MCP server."""
        # Tool name format: mcp__serverName__toolName.
        parts = prefixed_name.split("__")
        if len(parts) < 3:
            raise ValueError(f"Invalid MCP tool name: {prefixed_name}")
        server_name = parts[1]
        # Tool names may contain "__", so rejoin segments after the server name.
        tool_name = "__".join(parts[2:])  # tool name might contain __
        conn = self._connections.get(server_name)
        if not conn:
            raise RuntimeError(f"MCP server '{server_name}' not connected")
        return await conn.call_tool(tool_name, args)

    async def disconnect_all(self) -> None:
        """Disconnect all MCP servers and clear cached tools."""
        for conn in self._connections.values():
            conn.close()
        self._connections.clear()
        self._tools.clear()
        self._connected = False

    # ─── Config loading ──────────────────────────────────────

    def _load_configs(self) -> dict[str, dict]:
        """Load and merge MCP server config by priority."""
        merged: dict[str, dict] = {}

        # 1. Global config: ~/.miniagent/settings.json
        global_path = Path.home() / ".miniagent" / "settings.json"
        self._merge_config_file(global_path, merged)

        # 2. Project config: <cwd>/.miniagent/settings.json
        project_path = Path.cwd() / ".miniagent" / "settings.json"
        self._merge_config_file(project_path, merged)

        # 3. Claude Code convention: <cwd>/.mcp.json
        mcp_json_path = Path.cwd() / ".mcp.json"
        self._merge_config_file(mcp_json_path, merged)

        return merged

    def _merge_config_file(self, path: Path, target: dict[str, dict]) -> None:
        """Merge mcpServers from one config file into target."""
        if not path.exists():
            return
        try:
            raw = json.loads(path.read_text())
            # Supported formats:
            # 1. {"mcpServers": {"name": {...}}}
            # 2. {"name": {...}}
            servers = raw.get("mcpServers", raw)
            for name, config in servers.items():
                # Accept only objects with a command field; ignore invalid entries.
                if isinstance(config, dict) and "command" in config:
                    target[name] = config
        except Exception:
            # Skip malformed config files so one bad file does not break startup.
            pass
