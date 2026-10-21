"""Docker task workspace: no host mounts, no credentials, no local fallback.

Architecture references (not vendored code): SWE-ReX DockerDeployment and
AutoGen DockerCommandLineCodeExecutor. Only the Docker CLI is required.
"""

from __future__ import annotations

import asyncio
import base64
import fnmatch
import json
import os
from pathlib import Path, PurePosixPath
import stat
import subprocess
import tempfile
import uuid

from .stuck_detector import ToolResult

FILE_TOOLS = {"read_file", "write_file", "edit_file", "list_files", "grep_search", "run_shell"}
SANDBOX_TOOLS = FILE_TOOLS | {"agent", "compact_context", "enter_plan_mode", "exit_plan_mode"}
IMAGE = "miniagent-sandbox:1"
MAX_FILE = 2 * 1024 * 1024
MAX_TOTAL = 32 * 1024 * 1024
MAX_FILES = 5000
EXCLUDED = {
    ".git",
    ".task-skills",
    ".miniagent",
    ".mcp.json",
    ".ssh",
    ".aws",
    ".azure",
    ".config",
    ".venv",
    ".venv-agentdojo",
    "venv",
    "node_modules",
    "__pycache__",
    ".pytest_cache",
    ".idea",
    "artifacts",
    ".npmrc",
    ".pypirc",
    ".netrc",
    "id_rsa",
    "id_ed25519",
    "credentials.json",
}


def permitted_name(name: str) -> bool:
    path = PurePosixPath(name)
    return (
        bool(name)
        and not path.is_absolute()
        and str(path) == name
        and not any(
            part in {".", ".."}
            or part in EXCLUDED
            or part.startswith(".env")
            or part.endswith((".pem", ".key", ".p12", ".pfx", ".pyc"))
            for part in path.parts
        )
        and not any(ord(c) < 32 for c in name)
        and "\\" not in name
    )


def snapshot_source(root: Path) -> dict:
    """Copy ordinary files, honouring gitignore when the directory is a Git repo."""
    result = subprocess.run(
        [
            "git",
            "-c",
            "core.fsmonitor=false",
            "-C",
            str(root),
            "ls-files",
            "-co",
            "--exclude-standard",
            "-z",
            "--",
            ".",
        ],
        capture_output=True,
        timeout=10,
    )
    if result.returncode == 0:
        names = sorted(set(os.fsdecode(n) for n in result.stdout.split(b"\0") if n))
    else:
        names = []
        for directory, dirs, files in os.walk(root, followlinks=False):
            dirs[:] = [
                d
                for d in dirs
                if permitted_name((Path(directory) / d).relative_to(root).as_posix())
                and not (Path(directory) / d).is_symlink()
            ]
            names.extend((Path(directory) / f).relative_to(root).as_posix() for f in files)
    ignore_file = root / ".sandboxignore"
    patterns = []
    if ignore_file.is_file() and not ignore_file.is_symlink():
        patterns = [
            line.strip()
            for line in ignore_file.read_text().splitlines()
            if line.strip() and not line.lstrip().startswith("#")
        ]
    entries, total = {}, 0
    for name in names:
        if any(
            name.startswith(pattern) if pattern.endswith("/") else fnmatch.fnmatchcase(name, pattern)
            for pattern in patterns
        ):
            continue
        if not permitted_name(name):
            continue
        path = root / name
        if any(p.is_symlink() for p in [path, *path.parents] if p != root and p.is_relative_to(root)):
            continue
        # O_NOFOLLOW avoids following a file replaced with a symlink during copying.
        try:
            fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        except FileNotFoundError:
            continue
        with os.fdopen(fd, "rb") as stream:
            info = os.fstat(stream.fileno())
            if not stat.S_ISREG(info.st_mode):
                continue
            content = stream.read(MAX_FILE + 1)
        total += len(content)
        if len(content) > MAX_FILE or total > MAX_TOTAL or len(entries) >= MAX_FILES:
            raise ValueError(
                f"Snapshot limit reached at {name!r} (2 MiB/file, 32 MiB, 5000 files); exclude large inputs in .sandboxignore"
            )
        entries[name] = {
            "content": base64.b64encode(content).decode(),
            "executable": bool(info.st_mode & 0o111),
        }
    return entries


def validate_snapshot(entries: dict) -> dict:
    """Treat everything returned by the execution container as untrusted data."""
    if not isinstance(entries, dict) or len(entries) > MAX_FILES:
        raise ValueError("Invalid snapshot")
    validated, total = {}, 0
    for name, entry in entries.items():
        if not permitted_name(name):
            raise ValueError(f"Unsafe export path: {name!r}")
        content = base64.b64decode(entry["content"], validate=True)
        total += len(content)
        if len(content) > MAX_FILE or total > MAX_TOTAL or type(entry["executable"]) is not bool:
            raise ValueError("Invalid snapshot size or mode")
        validated[name] = {"content": base64.b64encode(content).decode(), "executable": entry["executable"]}
    return validated


class DockerWorkspace:
    def __init__(self, source: Path, output_root: Path, image: str = IMAGE):
        self.source = source.resolve()
        self.name = "miniagent-" + uuid.uuid4().hex
        self.image = image
        self.output = output_root.resolve() / self.name
        if self.output.is_relative_to(self.source):
            raise ValueError("Results must be outside the source repository")
        self.before = {}
        self.active = False
        self.lock = asyncio.Lock()

    async def _docker(self, *args, data=None, timeout=30, limit=48 * 1024 * 1024):
        process = await asyncio.create_subprocess_exec(
            "docker",
            *args,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )

        async def read_bounded(stream):
            chunks, size = [], 0
            while chunk := await stream.read(65536):
                size += len(chunk)
                if size > limit:
                    raise RuntimeError("Docker response exceeded size limit")
                chunks.append(chunk)
            return b"".join(chunks)

        async def send():
            try:
                if data:
                    process.stdin.write(data)
                    await process.stdin.drain()
            except (BrokenPipeError, ConnectionResetError):
                pass
            finally:
                process.stdin.close()

        tasks = [
            asyncio.create_task(send()),
            asyncio.create_task(read_bounded(process.stdout)),
            asyncio.create_task(read_bounded(process.stderr)),
            asyncio.create_task(process.wait()),
        ]
        try:
            _, stdout, stderr, code = await asyncio.wait_for(asyncio.gather(*tasks), timeout)
        except BaseException:
            for task in tasks:
                task.cancel()
            if process.returncode is None:
                process.kill()
            await process.wait()
            await asyncio.gather(*tasks, return_exceptions=True)
            raise
        if code:
            raise RuntimeError(f"Docker {args[0]} failed: {stderr.decode(errors='replace')[:2000]}")
        return stdout

    async def start(self, resources=None):
        self.before = await asyncio.to_thread(snapshot_source, self.source)
        self.output.mkdir(parents=True, exist_ok=False)
        try:
            await self._docker("info", "--format", "{{.ServerVersion}}")
            await self._docker("image", "inspect", self.image)
            await self._docker(
                "run",
                "-d",
                "--name",
                self.name,
                "--network",
                "none",
                "--read-only",
                "--user",
                "10001:10001",
                "--cap-drop",
                "ALL",
                "--security-opt",
                "no-new-privileges:true",
                "--pids-limit",
                "64",
                "--memory",
                "512m",
                "--memory-swap",
                "512m",
                "--cpus",
                "1",
                "--log-driver",
                "none",
                "--ulimit",
                "nofile=256:256",
                "--tmpfs",
                "/workspace:rw,nosuid,nodev,size=256m,uid=10001,gid=10001,mode=0700",
                "--tmpfs",
                "/tmp:rw,nosuid,nodev,size=64m,uid=10001,gid=10001,mode=0700",
                self.image,
            )
            self.active = True
            await self._request({"operation": "import", "files": self.before})
            if resources:
                await self._request({"operation": "import", "files": resources})
        except BaseException:
            await self.close()
            raise

    async def _request(self, request):
        if not self.active:
            raise RuntimeError("Sandbox unavailable; refusing local execution")
        try:
            raw = await self._docker(
                "exec",
                "-i",
                self.name,
                "python",
                "-I",
                "-B",
                "/opt/miniagent-worker.py",
                data=json.dumps(request).encode(),
                timeout=130,
            )
            response = json.loads(raw)
            if response.get("fatal"):
                raise RuntimeError(response["fatal"])
        except BaseException:
            await self.close()
            raise
        if "error" in response:
            raise ValueError(response["error"])
        return response

    @staticmethod
    def normalize(inp):
        inp = dict(inp)
        for key in ("path", "file_path"):
            if key in inp:
                value = inp[key] or "."
                path = PurePosixPath(value)
                if ".." in path.parts:
                    raise ValueError("Parent traversal is not allowed")
                if path.is_absolute():
                    path.relative_to("/workspace")
                else:
                    path = PurePosixPath("/workspace") / path
                inp[key] = str(path)
        return inp

    async def prepare(self, tool, inp):
        async with self.lock:
            result = await self._request({"operation": "prepare", "tool": tool, "input": self.normalize(inp)})
            return result["proposal"]

    async def execute(self, tool, inp, read_state, proposal=None):
        if tool not in FILE_TOOLS:
            raise ValueError("Unsupported sandbox tool")
        inp = self.normalize(inp)
        request = {
            "operation": "execute",
            "tool": tool,
            "input": inp,
            "read_hash": read_state.get(inp.get("file_path")),
        }
        if proposal:
            request["expected_hash"] = proposal["before_hash"]
        async with self.lock:
            response = await self._request(request)
        if "hash" in response:
            read_state[response["path"]] = response["hash"]
        result = ToolResult(
            response["result"],
            "error" if response.get("exit_code", 0) else "success",
            changed=response.get("changed", False),
        )
        result.fingerprint = response.get("result_fingerprint")
        return result

    async def export_changes(self):
        async with self.lock:
            response = await self._request({"operation": "snapshot"})
        # Runtime caches and credential-like filenames are never exported.
        after = validate_snapshot({k: v for k, v in response["files"].items() if permitted_name(k)})
        changed = {name: entry for name, entry in after.items() if self.before.get(name) != entry}
        deleted = sorted(self.before.keys() - after.keys())
        (self.output / "changes.json").write_text(
            json.dumps({"files": changed, "deleted": deleted}, indent=2)
        )
        # Only materialize validated regular files in a fresh host directory. No archive extraction.
        with tempfile.TemporaryDirectory(prefix="miniagent-diff-") as directory:
            root = Path(directory)
            for label, entries in [("before", self.before), ("after", after)]:
                (root / label).mkdir()
                for name, entry in entries.items():
                    path = root / label / name
                    path.parent.mkdir(parents=True, exist_ok=True)
                    path.write_bytes(base64.b64decode(entry["content"]))
                    path.chmod(0o755 if entry["executable"] else 0o644)
            result = await asyncio.to_thread(
                subprocess.run,
                [
                    "git",
                    "-c",
                    "core.quotePath=true",
                    "diff",
                    "--no-index",
                    "--binary",
                    "--no-ext-diff",
                    "--no-textconv",
                    "--",
                    "before",
                    "after",
                ],
                cwd=root,
                capture_output=True,
                timeout=30,
            )
            if result.returncode not in (0, 1):
                raise RuntimeError("Could not generate changes.patch")
            (self.output / "changes.patch").write_bytes(result.stdout)
        return {"changed": sorted(changed), "deleted": deleted, "output": str(self.output)}

    async def close(self):
        self.active = False
        # Cleanup is attempted even if docker run failed after creating the container.
        try:
            await self._docker("rm", "-f", self.name, timeout=15)
        except (OSError, RuntimeError, TimeoutError) as exc:
            # Never fall back to local execution. Surface cleanup failure for inspection.
            if "No such container" not in str(exc):
                import warnings

                warnings.warn(f"Could not remove {self.name}: {exc}", RuntimeWarning)
