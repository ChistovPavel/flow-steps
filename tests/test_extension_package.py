"""Test the distribution as an extracted Qwen extension, not just source files."""
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import tempfile
import unittest
import zipfile

from tools.build_extension import build, SOURCE


class ExtensionPackageTests(unittest.TestCase):
    def test_archive_runs_after_relocation_with_only_bundled_dependencies(self):
        with tempfile.TemporaryDirectory(prefix="qwen-extension ") as temporary:
            root = Path(temporary)
            archive_path = build(root / "extension.zip")
            installed = root / "installed elsewhere" / "flow-steps"
            with zipfile.ZipFile(archive_path) as archive:
                self.assertIsNone(archive.testzip())
                names = archive.namelist()
                self.assertIn("qwen-extension.json", names)
                self.assertNotIn("flow-steps/qwen-extension.json", names)
                self.assertFalse(any("__pycache__" in name or name.startswith(("tests/", "tools/", ".flows/")) for name in names))
                archive.extractall(installed)
            manifest = json.loads((installed / "qwen-extension.json").read_text(encoding="utf-8"))
            skills = installed / manifest["skills"]
            discovered = sorted(path.parent.name for path in skills.glob("*/SKILL.md"))
            self.assertEqual(discovered, ["fs-confirm", "fs-run"])
            self.assertEqual(manifest["name"], "flow-steps")
            script = skills / "fs-run/scripts/flow.py"
            self.assertTrue((skills / "fs-confirm/../fs-run/scripts/flow.py").resolve().is_file())
            vendor = script.parent / "vendor"
            provenance = json.loads((vendor / "provenance.json").read_text(encoding="utf-8"))
            for name, checksum in provenance["files"].items():
                self.assertEqual(hashlib.sha256((vendor / name).read_bytes()).hexdigest(), checksum)

            project = root / "проект с пробелами"
            project.mkdir()
            environment = {**os.environ, "GIT_CONFIG_GLOBAL": os.devnull,
                           "GIT_CONFIG_SYSTEM": os.devnull, "GIT_CONFIG_NOSYSTEM": "1",
                           "GIT_AUTHOR_NAME": "Package test", "GIT_AUTHOR_EMAIL": "test@example.invalid",
                           "GIT_COMMITTER_NAME": "Package test", "GIT_COMMITTER_EMAIL": "test@example.invalid"}

            def git(*args):
                return subprocess.run(["git", *args], cwd=project, env=environment,
                                      check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)

            def engine(*args, data=None):
                result = subprocess.run([sys.executable, "-I", "-B", str(script), "--project", str(project), *args],
                                        cwd=project, env=environment, input=data,
                                        stdout=subprocess.PIPE, stderr=subprocess.PIPE)
                self.assertEqual(result.returncode, 0, result.stderr.decode("utf-8", "replace") + result.stdout.decode("utf-8"))
                return json.loads(result.stdout.decode("utf-8"))

            git("init", "-q")
            (project / ".gitignore").write_text("/.flows/\n", encoding="utf-8")
            (project / "flows").mkdir()
            (project / "flows/test.yaml").write_text("name: package\nstart: work\nsteps:\n  work:\n    prompt: Do work\n", encoding="utf-8")
            git("add", "--all")
            git("commit", "-qm", "Setup")
            created = engine("create")
            started = engine("begin", "--token", created["token"])
            self.assertEqual(started["action"], "execute_step")
            (project / "result.txt").write_text("Result", encoding="utf-8")
            asked = engine("confirm", "--token", started["token"])
            self.assertEqual(asked["action"], "ask_commit_message")
            completed = engine("confirm", "--token", asked["token"], "--message-stdin", data="Проверка пакета".encode("utf-8"))
            self.assertEqual(completed["action"], "completed")
            self.assertEqual(git("status", "--porcelain").stdout, b"")
            self.assertFalse(list(installed.rglob("__pycache__")))

    def test_package_build_is_reproducible(self):
        with tempfile.TemporaryDirectory() as temporary:
            first = build(Path(temporary) / "first.zip")
            second = build(Path(temporary) / "second.zip")
            self.assertEqual(first.read_bytes(), second.read_bytes())

    def test_local_documentation_links_survive_skill_move(self):
        for document in SOURCE.rglob("*.md"):
            text = document.read_text(encoding="utf-8")
            for reference in re.findall(r"\]\(([^)]+)\)", text):
                if "://" in reference or reference.startswith("#"):
                    continue
                target = (document.parent / reference.split("#", 1)[0]).resolve()
                self.assertTrue(target.is_relative_to(SOURCE.resolve()), (document, reference))
                self.assertTrue(target.is_file(), (document, reference))


if __name__ == "__main__":
    unittest.main()
