"""Portable local attachment tests with real CLI subprocesses and SQLite.

No browser, listening socket, network, live model or external service is used.
"""
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tempfile
import unittest

from forge import __version__
from forge.cli import initialize
from forge.core import ForgeError, project_config
from forge.portable import BEGIN, END, SHIM, attach, stable_identity
from forge.telemetry import project_identity, snapshot


ROOT = Path(__file__).resolve().parents[2]


class PortableTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        result = subprocess.run([sys.executable, str(ROOT / "build.py")], cwd=ROOT,
                                capture_output=True, text=True, timeout=20)
        if result.returncode:
            raise AssertionError(result.stdout + result.stderr)
        cls.archive = ROOT / "dist/forge-3.1.0.pyz"

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="forge-portable-tests-")
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name).resolve()
        self.project = self.base / "Original Project"
        self.project.mkdir()
        self.engine = self.project / "FORGE/dist/forge-3.1.0.pyz"
        self.engine.parent.mkdir(parents=True)
        shutil.copyfile(self.archive, self.engine)

    def invoke(self, root, *args, expected=0):
        env = dict(os.environ)
        env.pop("PYTHONPATH", None)
        result = subprocess.run([sys.executable, str(root / ".forge/forge.py"), *map(str, args)],
                                cwd=self.base, env=env, capture_output=True, text=True, timeout=20)
        self.assertEqual(result.returncode, expected, result.stdout + result.stderr)
        self.assertNotIn("Traceback", result.stderr)
        return json.loads(result.stdout) if expected == 0 else result

    def write_config(self, **overrides):
        config = {"schema": 1, "engine_version": __version__, "name": "Existing name"}
        config.update(overrides)
        (self.project / "forge.json").write_text(json.dumps(config))
        return config

    def test_attach_preserves_existing_project_data_and_unmanaged_instructions(self):
        existing = {
            "AGENTS.md": "# User contract\n\nKeep these instructions.  \n\n\n",
            "PROJECT.md": "# Existing project\nAlready scoped.\n",
            "acceptance.json": '{"criteria":["user-owned"]}\n',
            "memory/NOW.md": "Current user work in progress.\n",
            "src/user.py": "print('preserve')\n",
        }
        for relative, content in existing.items():
            path = self.project / relative
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(content)
        result = attach(self.project, self.engine)
        self.assertEqual(result["status"], "attached")
        for relative, content in existing.items():
            current = (self.project / relative).read_text()
            if relative == "AGENTS.md":
                self.assertTrue(current.startswith(content), "User bytes outside the managed block must be preserved")
                self.assertIn(BEGIN, current)
            else:
                self.assertEqual(current, content)
        with (self.project / "AGENTS.md").open("a") as file:
            file.write("\nUser addition after managed block.\n")
        before = (self.project / "AGENTS.md").read_bytes()
        second = attach(self.project, self.engine)
        self.assertEqual((self.project / "AGENTS.md").read_bytes(), before)
        self.assertEqual(before.decode().count(BEGIN), 1)
        self.assertEqual(before.decode().count(END), 1)
        self.assertEqual(second["project"]["id"], result["project"]["id"])
        self.assertEqual((self.project / ".forge/forge.py").read_text(), SHIM)

    def test_fresh_attachments_and_fresh_init_have_unique_persisted_ids(self):
        left = attach(self.project, self.engine)["project"]["id"]
        other = self.base / "Other Project"
        other.mkdir()
        right = attach(other, self.engine)["project"]["id"]
        first_init, second_init = self.base / "Init One", self.base / "Init Two"
        initialize(first_init, "One", "Build one")
        initialize(second_init, "Two", "Build two")
        ids = [left, right, stable_identity(first_init), stable_identity(second_init)]
        self.assertEqual(len(set(ids)), 4)
        self.assertTrue(all(re.fullmatch("[a-f0-9]{32}", item) for item in ids))
        self.assertEqual(stable_identity(self.project), left)
        self.assertEqual(json.loads((self.project / "forge.json").read_text())["project_id"], left)

    def test_legacy_stable_identity_upgrades_once_preserving_other_configuration(self):
        original = self.write_config(brief="Existing brief", user_field={"preserved": True})
        legacy = project_identity(self.project)["id"]
        self.assertEqual(len(legacy), 16)
        assigned = stable_identity(self.project)
        self.assertEqual(stable_identity(self.project), assigned)
        stored = json.loads((self.project / "forge.json").read_text())
        self.assertEqual({k: v for k, v in stored.items() if k != "project_id"}, original)
        self.assertEqual(project_identity(self.project)["id"], assigned)

    def test_rename_unicode_spaces_keeps_identity_history_and_real_relative_cli(self):
        attached = attach(self.project, self.engine)
        original_id = attached["project"]["id"]
        emitted = self.invoke(self.project, "event", ".", "--type", "session.started", "--agent", "before-rename", "--label", "Actual local CLI event")
        self.assertEqual(emitted["project"]["id"], original_id)
        renamed = self.base / "新项目 with spaces 🧮"
        self.project.rename(renamed)
        self.assertEqual(project_identity(renamed), {"id": original_id, "name": renamed.name})
        self.assertEqual(project_config(renamed)["name"], renamed.name)
        self.assertEqual(snapshot(renamed)["agents"][0]["id"], "before-rename")
        hint = json.loads((renamed / ".forge/engine.json").read_text())
        self.assertFalse(Path(hint["engine_relative"]).is_absolute())
        self.assertTrue((renamed / hint["engine_relative"]).is_file())
        after = self.invoke(renamed, "event", ".", "--type", "agent.progress", "--agent", "before-rename", "--label", "After rename")
        self.assertEqual(after["project"], {"id": original_id, "name": renamed.name})
        doctor = self.invoke(renamed, "doctor", ".")
        self.assertEqual(doctor["project"], renamed.name)
        self.assertEqual(snapshot(renamed)["stats"]["events"], 2)

    def test_moving_whole_capsule_preserves_engine_hint_and_executes(self):
        original_id = attach(self.project, self.engine)["project"]["id"]
        destination_parent = self.base / "Different parent"
        destination_parent.mkdir()
        moved = destination_parent / "Moved capsule"
        shutil.move(str(self.project), str(moved))
        hint = json.loads((moved / ".forge/engine.json").read_text())
        self.assertEqual(hint["engine_relative"], "FORGE/dist/forge-3.1.0.pyz")
        self.assertEqual(project_identity(moved)["id"], original_id)
        result = self.invoke(moved, "event", ".", "--type", "idle")
        self.assertEqual(result["project"]["id"], original_id)
        self.assertEqual(result["project"]["name"], "Moved capsule")

    def test_explicit_name_is_preserved_after_folder_rename(self):
        attach(self.project, self.engine, name="Stable product name")
        renamed = self.base / "Different folder name"
        self.project.rename(renamed)
        self.assertEqual(project_config(renamed)["name"], "Stable product name")
        self.assertEqual(self.invoke(renamed, "intake", ".")["known"]["name"], "Stable product name")

    def test_cli_attach_works_against_existing_directory(self):
        env = dict(os.environ)
        env.pop("PYTHONPATH", None)
        result = subprocess.run([sys.executable, str(self.archive), "attach", str(self.project), "--engine", str(self.engine)],
                                cwd=self.base, env=env, capture_output=True, text=True, timeout=20)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(json.loads(result.stdout)["status"], "attached")
        self.assertEqual(self.invoke(self.project, "intake", ".")["known"]["name"], self.project.name)

    def test_bad_pin_schema_or_persistent_id_preserves_files_before_rejection(self):
        for overrides in [{"schema": 9}, {"schema": True}, {"engine_version": "2.0.0"},
                          {"project_id": "../unsafe"}, {"project_id": 123}, {"project_id": ""}]:
            with self.subTest(overrides=overrides):
                self.write_config(**overrides)
                before = (self.project / "forge.json").read_bytes()
                with self.assertRaises(ForgeError): attach(self.project, self.engine)
                self.assertEqual((self.project / "forge.json").read_bytes(), before)
                self.assertFalse((self.project / "AGENTS.md").exists())
                self.assertFalse((self.project / ".forge").exists())

    def test_bad_existing_shim_is_not_overwritten_or_partially_attached(self):
        state = self.project / ".forge"
        state.mkdir()
        shim = state / "forge.py"
        shim.write_text("print('user script')\n")
        with self.assertRaises(ForgeError): attach(self.project, self.engine)
        self.assertEqual(shim.read_text(), "print('user script')\n")
        self.assertFalse((self.project / "forge.json").exists())
        self.assertFalse((self.project / "AGENTS.md").exists())

    def test_ambiguous_managed_blocks_preserve_existing_bytes(self):
        agents = self.project / "AGENTS.md"
        for content in [BEGIN + "\nmissing end\n", END + BEGIN, BEGIN + END + BEGIN + END]:
            with self.subTest(content=content):
                agents.write_text(content)
                with self.assertRaises(ForgeError): attach(self.project, self.engine)
                self.assertEqual(agents.read_text(), content)
                self.assertFalse((self.project / "forge.json").exists())

    def test_symlink_setup_targets_root_and_engine_are_rejected(self):
        outside = self.base / "outside.txt"
        outside.write_text("must remain unchanged")
        for relative in ["forge.json", "AGENTS.md", "PROJECT.md", "acceptance.json", "memory/NOW.md", ".forge/engine.json", ".forge/forge.py"]:
            with self.subTest(relative=relative):
                target = self.project / relative
                target.parent.mkdir(parents=True, exist_ok=True)
                target.symlink_to(outside)
                with self.assertRaises(ForgeError): attach(self.project, self.engine)
                self.assertEqual(outside.read_text(), "must remain unchanged")
                self.assertTrue(target.is_symlink())
                target.unlink()
        alias = self.base / "project alias"
        alias.symlink_to(self.project, target_is_directory=True)
        with self.assertRaises(ForgeError): attach(alias, self.engine)
        engine_alias = self.base / "engine alias.pyz"
        engine_alias.symlink_to(self.engine)
        with self.assertRaises(ForgeError): attach(self.project, engine_alias)
        self.assertFalse((self.project / "forge.json").exists())


if __name__ == "__main__":
    unittest.main()
