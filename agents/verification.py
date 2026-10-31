"""Controller-owned verification of an exported candidate in a fresh sandbox."""

import asyncio
import json

from .workspace import DockerWorkspace, validate_snapshot


async def verify_candidate(workspace, command, timeout=120):
    verifier = DockerWorkspace(workspace.source, workspace.output / "verification", workspace.image)
    verifier.protected_paths = list(workspace.protected_paths)
    result = {"status": "error", "command": command}
    try:
        async with asyncio.timeout(timeout):
            changes = json.loads((workspace.output / "changes.json").read_text())
            snapshot = dict(workspace.before)
            for name in changes["deleted"]:
                if workspace._is_protected(name):
                    raise ValueError("Cannot delete a protected acceptance file")
                snapshot.pop(name, None)
            for name, entry in validate_snapshot(changes["files"]).items():
                if workspace._is_protected(name):
                    raise ValueError("Cannot modify a protected acceptance file")
                snapshot[name] = entry
            await verifier.start(snapshot=snapshot)
            response = await verifier._request({
                "operation": "execute", "tool": "run_shell",
                "input": {"command": command, "timeout": min(timeout, 120) * 1000},
            })
            code = response["exit_code"]
            result.update(status="passed" if code == 0 else "failed", exit_code=code,
                          output=response["result"])
    except Exception as exc:
        result.update(status="error", error=f"{type(exc).__name__}: {exc}")
    finally:
        try:
            async with asyncio.timeout(20):
                error = await verifier.close()
            if error:
                result.update(status="error", cleanup_error=error)
        except Exception as exc:
            result.update(status="error", cleanup_error=f"{type(exc).__name__}: {exc}")
    return result
