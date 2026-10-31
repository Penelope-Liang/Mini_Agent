"""Bounded action/result cycle detection without semantic progress claims."""

import unittest

from agents.stuck_detector import StuckDetector, ToolResult


class AlternatingCycleTests(unittest.TestCase):
    def test_cycles_warn_once_and_stop_after_complete_repetitions(self):
        for length in (2, 3, 4):
            with self.subTest(length=length):
                detector = StuckDetector()
                events = []
                for i in range(length * 4):
                    event = detector.observe('read_file', {'file_path': str(i % length)}, ToolResult('same'), 0)
                    if event:
                        events.append((i + 1, event))
                self.assertEqual(events, [
                    (length * 3, {'action': 'warn', 'pattern': 'alternating_cycle', 'count': 3, 'cycle_length': length}),
                    (length * 4, {'action': 'stop', 'pattern': 'alternating_cycle', 'count': 4, 'cycle_length': length}),
                ])

    def test_changed_arguments_results_and_status_break_cycles(self):
        for changing in ('arguments', 'result', 'status'):
            detector = StuckDetector()
            for i in range(8):
                args = {'file_path': str(i % 2)}
                text, status = 'same', 'success'
                if i == 4:
                    if changing == 'arguments':
                        args['file_path'] = 'new'
                    elif changing == 'result':
                        text = 'same prefix, new error at the end'
                    else:
                        status = 'error'
                self.assertIsNone(detector.observe('read_file', args, ToolResult(text, status), 0))

    def test_file_round_trips_detected_but_new_content_is_not(self):
        for progressing in (False, True):
            detector = StuckDetector()
            events = []
            version = 0
            for i in range(16):
                if i % 2 == 0:
                    result = ToolResult('failed', 'error')
                    name, args = 'run_shell', {'command': 'pytest'}
                else:
                    step = i // 2
                    before, after = (str(step), str(step + 1)) if progressing else (('a', 'b') if step % 2 == 0 else ('b', 'a'))
                    result = ToolResult('Successfully wrote config', changed=True)
                    result.content_transition = (before, after)
                    version += 1
                    name, args = 'write_file', {'file_path': 'config', 'content': after if not progressing else 'placeholder'}
                event = detector.observe(name, args, result, version)
                if event:
                    events.append((i + 1, event['action']))
            self.assertEqual(events, [] if progressing else [(12, 'warn'), (16, 'stop')])

    def test_compaction_preserves_history_and_untracked_change_breaks_it(self):
        detector = StuckDetector()
        for i in range(6):
            detector.observe('read_file', {'file_path': str(i % 2)}, ToolResult('same'), 0)
        detector.observe('compact_context', {'reason': 'fold'}, ToolResult('compacted'), 0)
        self.assertIsNone(detector.observe('read_file', {'file_path': '0'}, ToolResult('same'), 0))
        self.assertEqual(detector.observe('read_file', {'file_path': '1'}, ToolResult('same'), 0)['action'], 'stop')
        self.assertIsNone(detector.observe('read_file', {'file_path': '0'}, ToolResult('same'), 1))
        self.assertEqual(len(detector.cycle_history), 1)

    def test_history_is_bounded_and_thresholds_are_configurable(self):
        detector = StuckDetector(warn_at=2, stop_at=3)
        events = []
        for i in range(6):
            event = detector.observe('read_file', {'file_path': str(i % 2)}, ToolResult('same'), 0)
            if event:
                events.append((i + 1, event['action']))
        self.assertEqual(events, [(4, 'warn'), (6, 'stop')])
        for i in range(100):
            detector.observe('read_file', {'file_path': str(i)}, ToolResult('new'), 0)
        self.assertEqual(len(detector.cycle_history), 12)
