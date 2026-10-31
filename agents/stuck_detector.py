"""Task-local repetition detection, inspired by OpenHands StuckDetector.

Independent implementation; no framework dependency. Fingerprints stay in memory.
"""

import hashlib
import json
import re
from collections import Counter, deque


class ToolResult(str):
    """String-compatible result with execution metadata, preserved until collection."""

    def __new__(cls, text, status="success", *, changed=False, approved=False):
        result = super().__new__(cls, text)
        result.status = status
        result.changed = changed
        result.approved = approved
        result.fingerprint = None
        result.content_transition = None
        return result


def legacy_result(text):
    if isinstance(text, ToolResult):
        return text
    # Compatibility boundary for legacy handlers, not substring matching of file contents.
    status = (
        "denied"
        if text.startswith("Action denied:")
        else (
            "error"
            if text.startswith(("Error", "Warning:", "Unknown", "Command failed", "Command timed out"))
            else "success"
        )
    )
    return ToolResult(text, status)


class StuckDetector:
    def __init__(self, warn_at=3, stop_at=4, share_denials_with=None):
        if not isinstance(warn_at, int) or not isinstance(stop_at, int) or not 1 <= warn_at < stop_at:
            raise ValueError("Stuck thresholds must satisfy 1 <= warn_at < stop_at")
        self.warn_at, self.stop_at = warn_at, stop_at
        self.last_pair = self.last_error = None
        self.repeats = self.errors = 0
        # A user's refusals apply to the whole task, so sub-agents count against the parent's totals.
        shared = share_denials_with
        self.denials = shared.denials if shared is not None else Counter()
        self.tool_denials = shared.tool_denials if shared is not None else Counter()
        self.child_stops = Counter()
        self.neutral = {}
        self.cycle_history = deque(maxlen=4 * stop_at)
        self._cycle_version = None
        self._cycle_warning = None

    @staticmethod
    def call_key(name, args):
        args = dict(args)
        if name == "run_shell" and isinstance(args.get("command"), str):
            command = args["command"]
            # Collapse only simple unquoted horizontal whitespace; preserve shell quoting/escapes/newlines.
            if not any(c in command for c in "'\"\\\n\r"):
                args["command"] = " ".join(command.split())
        return hashlib.sha256(json.dumps([name, args], sort_keys=True, ensure_ascii=False).encode()).digest()

    @staticmethod
    def result_key(name, text):
        if name == "run_shell":
            # Normalize only pytest's summary duration, retaining test counts and all other output.
            text = re.sub(r"(?m)^(=+ .+? in )\d+(?:\.\d+)?s( =+)$", r"\1<duration>s\2", text)
        return hashlib.sha256(text.encode()).digest()

    def observe(self, name, args, result, version):
        cycle = self._observe_cycle(name, args, result, version)
        repeated = self._observe_repetition(name, args, result, version)
        if repeated and repeated['action'] == 'stop':
            return repeated
        return cycle or repeated

    def _observe_cycle(self, name, args, result, version):
        # A tracked write may revisit old content despite a monotonically increasing version.
        expected = self._cycle_version
        if expected is not None:
            expected += int(result.changed and result.content_transition is not None)
        if (expected is not None and version != expected) or result.status in {'denied', 'stopped'}:
            self.cycle_history.clear()
            self._cycle_warning = None
        self._cycle_version = version
        if name == 'compact_context' or result.status in {'denied', 'stopped'}:
            return None
        if result.changed and result.content_transition is None:
            self.cycle_history.clear()
            self._cycle_warning = None
            return None
        signature = (self.call_key(name, args), result.status,
                     result.fingerprint or self.result_key(name, str(result)), result.content_transition)
        self.cycle_history.append(signature)
        history = list(self.cycle_history)
        for length in range(2, 5):
            block = history[-length:]
            if len(block) != length or len(set(block)) < 2:
                continue
            count = 1
            while (count + 1) * length <= len(history):
                if history[-(count + 1) * length:-count * length] != block:
                    break
                count += 1
            event = self._event('alternating_cycle', count, self.warn_at, self.stop_at)
            if event is None:
                continue
            # Rotations are the same loop, not new warnings on every tool call.
            key = frozenset(tuple(block[i:] + block[:i]) for i in range(length))
            if event['action'] == 'warn' and key == self._cycle_warning:
                return None
            self._cycle_warning = key
            return {**event, 'cycle_length': length}
        self._cycle_warning = None
        return None

    def _observe_repetition(self, name, args, result, version):
        call = self.call_key(name, args)
        if result.approved:
            self.denials.pop(call, None)
            self.tool_denials.pop(name, None)
        if result.status == "denied":
            self.last_pair = self.last_error = None
            self.repeats = self.errors = 0
            self.denials[call] += 1
            self.tool_denials[name] += 1
            count = self.denials[call]
            event = self._event("repeated_denial", count, self.warn_at, self.stop_at)
            if event:
                return event
            return self._event(
                "tool_denial_total", self.tool_denials[name], self.warn_at * 2, self.stop_at * 2
            )
        if result.status == "stopped":
            # Counted per tool, not per call: varying the child prompt must not reset it.
            self.last_pair = self.last_error = None
            self.repeats = self.errors = 0
            self.child_stops[name] += 1
            return self._event("repeated_child_stop", self.child_stops[name], self.warn_at, self.stop_at)
        if name == "compact_context" or (
            result.status == "success" and name in {"write_file", "edit_file"} and not result.changed
        ):
            pair = (result.fingerprint or self.result_key(name, str(result)), version)
            previous, count = self.neutral.get(call, (None, 0))
            count = count + 1 if previous == pair else 1
            self.neutral[call] = (pair, count)
            return self._event("repeated_result", count, self.warn_at, self.stop_at)
        pair = (call, result.fingerprint or self.result_key(name, str(result)), version)
        self.repeats = self.repeats + 1 if pair == self.last_pair else 1
        self.last_pair = pair
        if result.status == "error":
            error = (call, version)
            self.errors = self.errors + 1 if error == self.last_error else 1
            self.last_error = error
        else:
            self.errors = 0
            self.last_error = None
        return self._event("repeated_error", self.errors, self.warn_at, self.stop_at) or self._event(
            "repeated_result", self.repeats, self.warn_at, self.stop_at
        )

    @staticmethod
    def _event(pattern, count, warn_at, stop_at):
        if count >= stop_at:
            return {"action": "stop", "pattern": pattern, "count": count}
        if count == warn_at:
            return {"action": "warn", "pattern": pattern, "count": count}
        return None
