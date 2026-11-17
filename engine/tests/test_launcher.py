"""Portable launcher/package checks using actual prepare-only subprocesses.

These tests attach real projects and invoke their CLI shims. They never bind a
server socket, open a browser, call a model or access the network. Prepared
commands are inspected, not represented as live Observatory connections.
"""
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest
import zipfile


ROOT = Path(__file__).resolve().parents[2]


class LauncherTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        built = subprocess.run([sys.executable, str(ROOT / "build.py")], cwd=ROOT,
                               capture_output=True, text=True, timeout=20)
        if built.returncode:
            raise AssertionError(built.stdout + built.stderr)
        cls.archive = ROOT / "dist/forge-3.1.0.pyz"

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="forge-launcher-tests-")
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name).resolve()
        self.project = self.base / "Project capsule"
        self.bundle = self.project / "FORGE"
        (self.bundle / "dist").mkdir(parents=True)
        shutil.copyfile(ROOT / "launch.py", self.bundle / "launch.py")
        shutil.copyfile(self.archive, self.bundle / "dist/forge-3.1.0.pyz")
        self.env = dict(os.environ)
        self.env.pop("PYTHONPATH", None)

    def prepare(self, *args, bundle=None, expected=0):
        bundle = bundle or self.bundle
        result = subprocess.run([sys.executable, str(bundle / "launch.py"), "--prepare-only", *map(str, args)],
                                cwd=self.base, env=self.env, capture_output=True, text=True, timeout=40)
        self.assertEqual(result.returncode, expected, result.stdout + result.stderr)
        self.assertNotIn("Traceback", result.stderr)
        if expected:
            self.assertEqual(result.stdout, "")
            return result
        parsed = json.loads(result.stdout)
        self.assertEqual(parsed["status"], "prepared_not_started")
        return parsed

    def assert_prepared_without_live_server(self, root):
        self.assertTrue((root / ".forge/engine.json").is_file())
        self.assertTrue((root / ".forge/forge.py").is_file())
        self.assertFalse((root / ".forge/observatory.json").exists())
        self.assertFalse((root / ".forge/state.sqlite").exists())

    def test_default_parent_is_correct_and_explicit_project_preserves_user_files(self):
        user = self.project / "PROJECT.md"
        user.write_bytes(b"User's existing brief.\r\n")
        result = self.prepare("--no-open")
        self.assertEqual(result["project"]["name"], self.project.name)
        self.assertEqual(user.read_bytes(), b"User's existing brief.\r\n")
        self.assertEqual(result["launch_command"][3], str(self.project))
        self.assertNotIn("--open", result["launch_command"])
        self.assert_prepared_without_live_server(self.project)
        other = self.base / "Explicit target"
        other.mkdir()
        (other / "user.txt").write_text("keep")
        explicit = self.prepare("--project", other)
        self.assertEqual(explicit["project"]["name"], "Explicit target")
        self.assertNotEqual(explicit["project"]["id"], result["project"]["id"])
        self.assertEqual((other / "user.txt").read_text(), "keep")
        self.assertIn("--open", explicit["launch_command"])
        self.assert_prepared_without_live_server(other)

    def test_rename_and_move_keep_id_port_and_update_current_paths(self):
        before = self.prepare("--no-open")
        old_path = str(self.project)
        new_parent = self.base / "New parent"
        new_parent.mkdir()
        renamed = new_parent / "项目 moved with spaces 🧮"
        shutil.move(str(self.project), str(renamed))
        after = self.prepare("--no-open", bundle=renamed / "FORGE")
        self.assertEqual(after["project"]["id"], before["project"]["id"])
        self.assertEqual(after["port"], before["port"])
        self.assertEqual(after["project"]["name"], renamed.name)
        self.assertNotIn(old_path, json.dumps(after))
        self.assertEqual(after["launch_command"][1], str(renamed / "FORGE/dist/forge-3.1.0.pyz"))
        self.assertEqual(after["launch_command"][3], str(renamed))
        hint = json.loads((renamed / ".forge/engine.json").read_text())
        self.assertEqual(hint["engine_relative"], "FORGE/dist/forge-3.1.0.pyz")
        self.assertEqual(after["event_command"][1:], [".forge/forge.py", "event", "."])

    def test_port_zero_override_is_prepared_not_falsely_live_and_no_open_is_respected(self):
        zero = self.prepare("--port", "0", "--no-open")
        self.assertEqual(zero["port"], 0)
        self.assertEqual(zero["launch_command"][-2:], ["--port", "0"])
        self.assert_prepared_without_live_server(self.project)
        chosen = self.prepare("--port", "54321")
        self.assertEqual(chosen["port"], 54321)
        self.assertEqual(chosen["launch_command"][-3:], ["--port", "54321", "--open"])
        default = self.prepare()
        expected_port = 18000 + int(default["project"]["id"][:8], 16) % 30000
        self.assertEqual(default["port"], expected_port)

    def test_invalid_ports_and_missing_project_fail_without_partial_setup(self):
        for port in ("-1", "65536", "not-a-number"):
            with self.subTest(port=port):
                self.prepare("--port", port, expected=2)
                self.assertFalse((self.project / "forge.json").exists())
        missing = self.base / "Missing project"
        result = self.prepare("--project", missing, expected=2)
        self.assertIn("existing directory", result.stderr)
        self.assertFalse(missing.exists())
        self.assertFalse((self.project / "forge.json").exists())

    def test_missing_shared_engine_fails_friendly(self):
        (self.bundle / "dist/forge-3.1.0.pyz").unlink()
        result = self.prepare(expected=2)
        self.assertIn("Shared engine missing", result.stderr)
        self.assertFalse((self.project / "forge.json").exists())

    def test_demo_is_separate_project_with_its_own_stable_id(self):
        fixture = ROOT / "samples/observatory-demo"
        if not fixture.is_dir(): self.skipTest("Demo fixture not available")
        shutil.copytree(fixture, self.bundle / "samples/observatory-demo",
                        ignore=shutil.ignore_patterns("__pycache__", "*.pyc", "engine.json", "forge.py", "observatory.json"))
        outer = self.prepare("--no-open")
        demo = self.prepare("--demo", "--no-open")
        self.assertNotEqual(demo["project"]["id"], outer["project"]["id"])
        self.assertEqual(demo["project"]["name"], "Observatory Sandbox")
        self.assertEqual(self.prepare("--demo", "--no-open")["project"]["id"], demo["project"]["id"])
        self.assertEqual(demo["launch_command"][3], str(self.bundle / "samples/observatory-demo"))
        self.assertFalse((self.project / ".forge/observatory.json").exists())

    def test_release_layout_extract_rename_prepare_and_actual_event_shim(self):
        target = self.base / "release.zip"
        packaged = subprocess.run([sys.executable, str(ROOT / "package_release.py"), str(target)], cwd=ROOT,
                                  env=self.env, capture_output=True, text=True, timeout=60)
        self.assertEqual(packaged.returncode, 0, packaged.stdout + packaged.stderr)
        self.assertTrue(json.loads(packaged.stdout)["manifest_verified"])
        extracted = self.base / "Extracted parent"
        with zipfile.ZipFile(target) as archive:
            self.assertIsNone(archive.testzip())
            names = set(archive.namelist())
            for relative in ["AGENTS.md", "PROJECT.md", "forge.json", "acceptance.json", "memory/NOW.md",
                             "Start_Observatory.command", "Start_Demo.command", "README_FIRST.md",
                             "FORGE/launch.py", "FORGE/dist/forge-3.1.0.pyz"]:
                self.assertIn("FORGE_Project/" + relative, names)
            self.assertNotIn("FORGE_Project/.forge/state.sqlite", names)
            fresh = json.loads(archive.read("FORGE_Project/forge.json"))
            self.assertNotIn("project_id", fresh)
            self.assertEqual(fresh["name_source"], "folder")
            manifest = json.loads(archive.read("FORGE_Project/RELEASE_MANIFEST.json"))
            for relative, expected in manifest["files"].items():
                self.assertEqual(hashlib.sha256(archive.read("FORGE_Project/" + relative)).hexdigest(), expected)
            archive.extractall(extracted)
        project = extracted / "FORGE_Project"
        renamed = extracted / "My polished product 项目"
        project.rename(renamed)
        prepared = self.prepare("--no-open", bundle=renamed / "FORGE")
        self.assertEqual(prepared["project"]["name"], renamed.name)
        self.assert_prepared_without_live_server(renamed)
        event = subprocess.run(prepared["event_command"] + ["--type", "session.started", "--agent", "actual-cli-test"],
                               cwd=renamed, env=self.env, capture_output=True, text=True, timeout=20)
        self.assertEqual(event.returncode, 0, event.stdout + event.stderr)
        result = json.loads(event.stdout)
        self.assertEqual(result["status"], "recorded")
        self.assertEqual(result["project"], prepared["project"])


if __name__ == "__main__":
    unittest.main()
