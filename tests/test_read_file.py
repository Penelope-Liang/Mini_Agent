import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from agents.agent import Agent
from agents.tools import _read_file


class ReadFileRangeTests(unittest.TestCase):
    def test_code_beyond_large_result_preview_can_be_read(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "large.py"
            path.write_text("\n".join(f"line_{i}: " + "x" * 80 for i in range(1, 914)))
            full = _read_file({"file_path": str(path)})
            with patch("agents.agent.Path.home", return_value=Path(directory)):
                preview = Agent._persist_large_result(None, "read_file", full)
                self.assertIn("offset=201, limit=200", preview)
                self.assertNotIn("line_821:", preview)

                section = _read_file({"file_path": str(path), "offset": 801, "limit": 100})
                self.assertIn(" 821 | line_821:", section)
                self.assertNotIn(" 800 |", section)
                self.assertIn("offset=901", section)
                self.assertEqual(Agent._persist_large_result(None, "read_file", section), section)

            last = _read_file({"file_path": str(path), "offset": 901, "limit": 100})
            self.assertIn(" 913 | line_913:", last)
            self.assertNotIn("More lines", last)
            self.assertTrue(_read_file({"file_path": str(path), "offset": 1000}).startswith("End of file"))

    def test_default_read_and_invalid_ranges(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "short.py"
            path.write_text("first\nsecond")
            self.assertEqual(_read_file({"file_path": str(path)}), "   1 | first\n   2 | second")
            for field in ("offset", "limit"):
                for value in (0, -1, True, "20"):
                    with self.subTest(field=field, value=value):
                        self.assertTrue(_read_file({"file_path": str(path), field: value}).startswith("Error"))


if __name__ == "__main__":
    unittest.main()
