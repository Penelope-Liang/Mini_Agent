"""Cancellable, terminal-only approval input for the sandbox controller."""

import asyncio
import os
import sys

from .ui import print_plan_approval_options, print_plan_for_approval


async def read_terminal_line(prompt: str, limit: int = 4096) -> str | None:
    """Return a bounded line, or None for EOF/unavailable input; never consume pipes."""
    if not sys.stdin.isatty():
        return None
    loop = asyncio.get_running_loop()
    answer = loop.create_future()
    fd = sys.stdin.fileno()
    data = bytearray()
    oversized = False

    def ready():
        nonlocal oversized
        if answer.done():
            return
        try:
            chunk = os.read(fd, 1)
            if not chunk or chunk == b'\n':
                answer.set_result(data.decode('utf-8', errors='replace') if chunk and not oversized else None)
            elif len(data) >= limit:
                oversized = True
            else:
                data.extend(chunk)
        except OSError:
            answer.set_result(None)

    try:
        loop.add_reader(fd, ready)
    except (OSError, ValueError, NotImplementedError):
        return None
    try:
        print(prompt, end='', flush=True)
        return await answer
    finally:
        loop.remove_reader(fd)


class TerminalApproval:
    def __init__(self):
        self.cancelled_while_waiting = False
        self._lock = asyncio.Lock()

    async def _read(self, prompt):
        try:
            return await read_terminal_line(prompt)
        except asyncio.CancelledError:
            self.cancelled_while_waiting = True
            raise

    async def __call__(self, _description: str) -> bool:
        async with self._lock:
            answer = await self._read('  Allow? [y/N]: ')
            return answer is not None and answer.strip().lower() in {'y', 'yes'}

    async def plan(self, content: str) -> dict:
        async with self._lock:
            print_plan_for_approval(content)
            print_plan_approval_options()
            while True:
                answer = await self._read('  Enter choice (1-3): ')
                if answer is None or answer.strip() == '3':
                    return {'choice': 'exit'}
                if answer.strip() == '1':
                    return {'choice': 'execute'}
                if answer.strip() == '2':
                    feedback = await self._read('  Feedback (what to change): ')
                    if feedback is None:
                        return {'choice': 'exit'}
                    if feedback.strip():
                        return {'choice': 'keep-planning', 'feedback': feedback.strip()}
                print('Choose 1, 2, or 3; revision feedback must not be empty.')
