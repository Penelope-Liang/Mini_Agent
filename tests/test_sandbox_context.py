import base64
import hashlib
import tempfile
from pathlib import Path
import unittest

from agents.sandbox_context import load_context


class ContextTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        self.source = self.root / "project-a"
        self.home = self.root / "home"
        self.source.mkdir()
        self.home.mkdir()

    def skill(self, name="review", body="Use ${CLAUDE_SKILL_DIR}/scripts/check.py"):
        directory = self.source / ".miniagent" / "skills" / name
        directory.mkdir(parents=True)
        (directory / "SKILL.md").write_text(body)
        return directory

    def load(self, names=(), query="login bug", **kwargs):
        return load_context(self.source, list(names), query, home=self.home, **kwargs)

    def test_selected_skill_resources_and_container_paths_only(self):
        directory = self.skill()
        (directory / "scripts").mkdir()
        (directory / "scripts/check.py").write_text("print('check')")
        (directory / "template.txt").write_text("template")
        # Unselected resources must not even be opened.
        other = self.skill("other")
        (other / "outside").symlink_to(self.home)
        context = self.load(["review"])
        self.assertEqual(len(context["files"]), 3)
        self.assertIn("/workspace/.task-skills/project/review/scripts/check.py", context["text"])
        self.assertNotIn(str(directory), context["text"])
        self.assertEqual(
            base64.b64decode(context["files"][".task-skills/project/review/template.txt"]["content"]),
            b"template",
        )

    def test_traversal_and_invalid_selectors_fail(self):
        for name in ["../review", "project:../review", "/tmp", "user:a/b", "unknown:review", "a\\b"]:
            with self.subTest(name=name), self.assertRaises(ValueError):
                self.load([name])

    def test_symlink_files_directories_and_parents_fail(self):
        directory = self.skill()
        link = directory / "outside"
        link.symlink_to(self.home, target_is_directory=True)
        with self.assertRaises(ValueError):
            self.load(["review"])
        link.unlink()
        link.symlink_to(directory / "SKILL.md")
        with self.assertRaises(ValueError):
            self.load(["review"])
        link.unlink()
        (directory.parent / "alias").symlink_to(directory, target_is_directory=True)
        with self.assertRaises(OSError):
            self.load(["alias"])
        directory.rename(self.root / "moved")
        directory.parent.rename(self.root / "skills")
        directory.parent.symlink_to(self.root / "skills", target_is_directory=True)
        with self.assertRaises(OSError):
            self.load(["review"])

    def test_user_scope_requires_explicit_selection(self):
        user = self.home / ".miniagent/skills/review"
        user.mkdir(parents=True)
        (user / "SKILL.md").write_text("user-only-content")
        self.skill(body="project-only-content")
        self.assertNotIn("user-only-content", self.load(["review"])["text"])
        self.assertIn("user-only-content", self.load(["user:review"])["text"])

    def test_allowed_tools_intersection_and_fork_rejection(self):
        self.skill(body="---\nallowed-tools: read_file, run_shell\n---\ncheck")
        self.skill("other", "---\nallowed-tools: read_file\n---\ncheck")
        self.assertEqual(self.load(["review", "other"])["allowed_tools"], {"read_file"})
        self.skill("fork", "---\ncontext: fork\n---\ncheck")
        with self.assertRaises(ValueError):
            self.load(["fork"])

    def memory_dir(self, project):
        key = hashlib.sha256(str(project.resolve()).encode()).hexdigest()[:16]
        directory = self.home / ".miniagent/projects" / key / "memory"
        directory.mkdir(parents=True)
        return directory

    def test_memory_is_project_scoped_read_only_and_relevance_filtered(self):
        a = self.memory_dir(self.source)
        b = self.memory_dir(self.root / "project-b")
        entry = "---\nname: login\ntype: project\n---\nlogin uses cookies"
        (a / "login.md").write_text(entry)
        (a / "unrelated.md").write_text("---\nname: billing\ntype: project\n---\ninvoices")
        (a / "legacy.md").write_text("login unclassified legacy data")
        (b / "secret.md").write_text(entry + " SECRET_PROJECT_B")
        before = {p: (p.read_bytes(), p.stat().st_mtime_ns) for p in self.home.rglob("*.md")}
        result = self.load()
        self.assertEqual(result["memories"], ["login.md"])
        self.assertNotIn("SECRET_PROJECT_B", result["text"])
        self.assertEqual(before, {p: (p.read_bytes(), p.stat().st_mtime_ns) for p in self.home.rglob("*.md")})
        self.assertEqual(self.load(memory=False)["memories"], [])

    def test_missing_memory_creates_nothing_and_symlink_memory_fails(self):
        self.assertEqual(self.load()["memories"], [])
        self.assertEqual(list(self.home.iterdir()), [])
        directory = self.memory_dir(self.source)
        (directory / "linked.md").symlink_to(self.root / "secret.md")
        with self.assertRaises(ValueError):
            self.load()

    def test_arguments_preserve_literal_backslashes(self):
        self.skill(body="Task: $ARGUMENTS")
        self.assertIn(r"\\g<1>", self.load(["review"], query=r"\g<1>")["text"])

    def test_oversized_skill_fails(self):
        directory = self.skill()
        (directory / "large").write_bytes(b"x" * (512 * 1024 + 1))
        with self.assertRaises(ValueError):
            self.load(["review"])
