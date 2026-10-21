"""Small stdlib-only tool worker. Runs inside the container, never on the host."""

import base64
import difflib
import fnmatch
import hashlib
import json
import os
from pathlib import Path
import re
import stat
import subprocess
import sys
import tempfile

ROOT = Path("/workspace")
MAX_FILE = 2 * 1024 * 1024
MAX_TOTAL = 32 * 1024 * 1024
MAX_FILES = 5000
MAX_OUTPUT = 50000


def path_for(raw):
    path = Path(raw)
    if ".." in path.parts:
        raise ValueError("Parent traversal is not allowed")
    if not path.is_absolute():
        path = ROOT / path
    path.relative_to(ROOT)
    current = ROOT
    for part in path.relative_to(ROOT).parts:
        current /= part
        if current.is_symlink():
            raise ValueError("Symbolic links are not supported")
    path.resolve().relative_to(ROOT.resolve())
    return path


def read_bytes(path):
    if not stat.S_ISREG(path.stat().st_mode):
        raise ValueError("Only regular files are supported")
    with path.open("rb") as stream:
        content = stream.read(MAX_FILE + 1)
    if len(content) > MAX_FILE:
        raise ValueError("File exceeds 2 MiB limit")
    return content


def fingerprint(path):
    return hashlib.sha256(read_bytes(path)).hexdigest() if path.exists() else None


def files():
    count = 0
    for directory, dirs, names in os.walk(ROOT, followlinks=False):
        dirs[:] = sorted(d for d in dirs if not (Path(directory) / d).is_symlink())
        for name in sorted(names):
            path = Path(directory) / name
            if path.is_symlink() or not stat.S_ISREG(path.lstat().st_mode):
                continue
            count += 1
            if count > MAX_FILES:
                raise ValueError("Workspace exceeds file count limit")
            yield path


def replacement(path, tool, inp):
    if tool == "write_file":
        after = inp["content"]
    else:
        before = read_bytes(path).decode("utf-8")
        old = inp["old_string"]
        if not old or before.count(old) != 1:
            raise ValueError("old_string must match exactly once")
        after = before.replace(old, inp["new_string"], 1)
    encoded = after.encode("utf-8")
    if len(encoded) > MAX_FILE:
        raise ValueError("File exceeds 2 MiB limit")
    return encoded


def handle(request):
    operation = request["operation"]
    if operation == "import":
        total = 0
        entries = request["files"]
        if len(entries) > MAX_FILES:
            raise ValueError("Too many files")
        for name, entry in entries.items():
            path = path_for(name)
            content = base64.b64decode(entry["content"], validate=True)
            total += len(content)
            if len(content) > MAX_FILE or total > MAX_TOTAL:
                raise ValueError("Snapshot size limit exceeded")
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(content)
            path.chmod(0o755 if entry["executable"] else 0o644)
        return {"result": "Workspace imported"}
    if operation == "snapshot":
        entries, total = {}, 0
        for path in files():
            content = read_bytes(path)
            total += len(content)
            if total > MAX_TOTAL:
                raise ValueError("Snapshot size limit exceeded")
            entries[path.relative_to(ROOT).as_posix()] = {
                "content": base64.b64encode(content).decode("ascii"),
                "executable": bool(path.stat().st_mode & 0o111),
            }
        return {"files": entries}

    tool, inp = request["tool"], request["input"]
    if tool in {"read_file", "write_file", "edit_file"}:
        path = path_for(inp["file_path"])
        before_hash = fingerprint(path)
        if operation == "prepare":
            after = replacement(path, tool, inp)
            old = read_bytes(path).decode("utf-8", errors="replace") if path.exists() else ""
            diff = "".join(
                difflib.unified_diff(
                    old.splitlines(True),
                    after.decode().splitlines(True),
                    fromfile=str(path),
                    tofile=str(path),
                )
            )
            return {
                "proposal": {
                    "path": str(path),
                    "before_hash": before_hash,
                    "after_hash": hashlib.sha256(after).hexdigest(),
                    "prompt": f"{tool}: {path}\n{diff[:MAX_OUTPUT]}",
                }
            }
        if tool == "read_file":
            content = read_bytes(path)
            if b"\0" in content[:8192]:
                return {"result": "Binary file, not readable as text."}
            offset, limit = inp.get("offset", 1), inp.get("limit")
            if (
                type(offset) is not int
                or offset < 1
                or (limit is not None and (type(limit) is not int or limit < 1))
            ):
                raise ValueError("offset and limit must be positive integers")
            lines = content.decode("utf-8", errors="replace").split("\n")
            if offset > len(lines):
                return {"result": f"End of file: {len(lines)} lines."}
            end = len(lines) if limit is None else min(len(lines), offset - 1 + limit)
            output = "\n".join(f"{i:4d} | {line}" for i, line in enumerate(lines[offset - 1 : end], offset))
            if end < len(lines):
                output += f"\n[More lines available. Continue with offset={end + 1}.]"
            return {"result": output[:MAX_OUTPUT], "path": str(path), "hash": before_hash}
        if before_hash is not None and request.get("read_hash") != before_hash:
            raise ValueError("Read this file before editing; it is unread or changed since the last read")
        if "expected_hash" in request and request["expected_hash"] != before_hash:
            raise ValueError("File changed after approval; read and approve it again")
        after = replacement(path, tool, inp)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(after)
        return {
            "result": f"Successfully wrote {path}",
            "path": str(path),
            "hash": hashlib.sha256(after).hexdigest(),
            "changed": before_hash != hashlib.sha256(after).hexdigest(),
        }
    if tool in {"list_files", "grep_search"}:
        base = path_for(inp.get("path") or ".")
        candidates = [base] if base.is_file() else files()
        output = []
        regex = re.compile(inp["pattern"]) if tool == "grep_search" else None
        for path in candidates:
            if not path.is_relative_to(base):
                continue
            relative = path.relative_to(ROOT).as_posix()
            if tool == "list_files":
                local = path.relative_to(base).as_posix()
                pattern = inp["pattern"]
                if fnmatch.fnmatch(local, pattern) or (
                    pattern.startswith("**/") and fnmatch.fnmatch(local, pattern[3:])
                ):
                    output.append(relative)
            elif not inp.get("include") or fnmatch.fnmatch(path.name, inp["include"]):
                content = read_bytes(path)
                if b"\0" not in content[:8192]:
                    for i, line in enumerate(content.decode("utf-8", errors="replace").splitlines(), 1):
                        if regex.search(line):
                            output.append(f"{relative}:{i}:{line}")
                            if len(output) >= 200:
                                break
            if len(output) >= 200:
                break
        return {"result": ("\n".join(output) or "No matches found.")[:MAX_OUTPUT]}
    if tool == "run_shell":
        timeout = min(max(float(inp.get("timeout", 30000)) / 1000, 0.1), 120)
        # Output goes to bounded container tmpfs, not an unbounded host pipe.
        with tempfile.TemporaryFile() as output:
            process = subprocess.Popen(
                ["/bin/sh", "-c", inp["command"]],
                cwd=ROOT,
                stdout=output,
                stderr=subprocess.STDOUT,
                start_new_session=True,
            )
            try:
                process.wait(timeout=timeout)
            except subprocess.TimeoutExpired:
                return {"fatal": "Command timed out; task container must be destroyed"}
            output.seek(0)
            digest = hashlib.sha256()
            size = 0
            while chunk := output.read(65536):
                digest.update(chunk)
                size += len(chunk)
            output.seek(0)
            text = output.read(MAX_OUTPUT).decode("utf-8", errors="replace")
        prefix = f"Command failed (exit code {process.returncode})\n" if process.returncode else ""
        return {
            "result": prefix + (text or "(no output)"),
            "exit_code": process.returncode,
            "result_fingerprint": digest.hexdigest() if size > MAX_OUTPUT else None,
        }
    raise ValueError(f"Tool not supported in sandbox: {tool}")


if __name__ == "__main__":
    try:
        request = json.loads(sys.stdin.buffer.read(48 * 1024 * 1024))
        response = handle(request)
    except Exception as exc:
        response = {"error": str(exc)}
    print(json.dumps(response))
