"""Core contracts, including real process ownership and crash release."""
from copy import deepcopy
from contextlib import closing
import hashlib
import json
import os
from pathlib import Path
import select
import sqlite3
import subprocess
import sys
import tempfile
import unittest

from forge import __version__
from forge.core import (
    ForgeError, State, atomic_json, canonical, load_json, number, output_hashes,
    project_config, project_lock, project_path, protect, relative_path,
    tree_hash, validate_plan, verify_protected,
)


ENGINE = str(Path(__file__).resolve().parents[1])


def plan():
    return {
        "schema": 1, "goal": "Build a useful artifact",
        "tasks": [
            {"id": "build", "brief": "Produce output", "outputs": ["result.txt"]},
            {"id": "document", "brief": "Explain output", "requires": ["build"],
             "outputs": ["README.md"]},
        ],
        "checks": [{"id": "verify", "argv": [sys.executable, "-c", "pass"]}],
    }


class ProjectFixture(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.project = self.root / "project"
        self.project.mkdir()


class CoreTests(ProjectFixture):
    def test_valid_plan_preserves_contract(self):
        value = plan()
        before = deepcopy(value)
        self.assertIs(validate_plan(value), value)
        self.assertEqual(value, before)

    def test_plan_invalid_top_level(self):
        for replacement in [None, [], {}, {**plan(), "schema": True},
                            {**plan(), "schema": 2}, {**plan(), "surprise": True},
                            {**plan(), "goal": " "}, {**plan(), "tasks": []},
                            {**plan(), "checks": []},
                            {**plan(), "visual_review_required": 1}]:
            with self.subTest(value=replacement), self.assertRaises(ForgeError):
                validate_plan(replacement)

    def test_dependencies_and_output_ownership(self):
        mutations = [
            lambda p: p["tasks"][0].update(requires=["document"]),
            lambda p: p["tasks"][0].update(requires=["build"]),
            lambda p: p["tasks"][1].update(id="build"),
            lambda p: p["tasks"][1].update(outputs=["result.txt"]),
            lambda p: p["tasks"][0].update(outputs=["result.txt", "result.txt"]),
            lambda p: p["tasks"][0].update(id="bad/id"),
            lambda p: p["tasks"][0].update(outputs=[]),
        ]
        for mutation in mutations:
            value = plan()
            mutation(value)
            with self.subTest(value=value), self.assertRaises(ForgeError):
                validate_plan(value)

    def test_invalid_checks_and_protected_paths(self):
        mutations = [
            lambda p: p["checks"][0].update(argv="python"),
            lambda p: p["checks"][0].update(argv=[]),
            lambda p: p["checks"][0].update(argv=["a\x00b"]),
            lambda p: p["checks"][0].update(timeout_seconds=True),
            lambda p: p["checks"][0].update(timeout_seconds=float("nan")),
            lambda p: p["checks"][0].update(timeout_seconds=601),
            lambda p: p["checks"].append(deepcopy(p["checks"][0])),
            lambda p: p.update(protected_files=["relative.py"]),
        ]
        for mutation in mutations:
            value = plan()
            mutation(value)
            with self.subTest(value=value), self.assertRaises(ForgeError):
                validate_plan(value)

    def test_number_bounds_and_types(self):
        self.assertEqual(number(2, 1, 3, "budget"), 2)
        for invalid in [True, None, "2", float("nan"), float("inf"), -1, 4, 10**1000]:
            with self.subTest(kind=type(invalid).__name__), self.assertRaises(ForgeError):
                number(invalid, 1, 3, "budget")

    def test_portable_paths_reject_traversal_reserved_and_null(self):
        self.assertEqual(relative_path("src/main.py"), "src/main.py")
        for invalid in ["", ".", "..", "a/../b", "/tmp/x", "a//b", "a/./b", "a/",
                        ".forge/state", "src/.git/config", "a\\b", None, "a\x00b"]:
            with self.subTest(value=invalid), self.assertRaises(ForgeError):
                relative_path(invalid)

    def test_project_outputs_reject_file_and_ancestor_symlinks(self):
        outside = self.root / "outside"
        outside.mkdir()
        (outside / "result").write_text("outside")
        (self.project / "link").symlink_to(outside, target_is_directory=True)
        (self.project / "file").symlink_to(outside / "result")
        for rel in ["link/result", "file", "../outside/result"]:
            with self.subTest(rel=rel), self.assertRaises(ForgeError):
                project_path(self.project, rel)

    def test_output_hashes_requires_regular_files(self):
        output = self.project / "output.txt"
        output.write_bytes(b"actual output")
        self.assertEqual(output_hashes(self.project, ["output.txt"]),
                         {"output.txt": hashlib.sha256(b"actual output").hexdigest()})
        for rel in ["missing", "directory"]:
            if rel == "directory":
                (self.project / rel).mkdir()
            with self.subTest(rel=rel), self.assertRaises(ForgeError):
                output_hashes(self.project, [rel])

    def test_tree_hash_detects_content_but_excludes_runtime_state(self):
        output = self.project / "output.txt"
        output.write_text("v1")
        first = tree_hash(self.project)
        (self.project / ".forge").mkdir()
        (self.project / ".forge" / "state").write_text("runtime")
        self.assertEqual(tree_hash(self.project), first)
        output.write_text("v2")
        self.assertNotEqual(tree_hash(self.project), first)
        (self.project / "alias").symlink_to(output)
        with self.assertRaises(ForgeError):
            tree_hash(self.project)

    def test_duplicate_json_keys_rejected_at_any_depth(self):
        path = self.root / "data.json"
        for content in ['{"schema":1,"schema":2}', '{"x":{"id":1,"id":2}}',
                        '{"x":NaN}', '{"x":Infinity}', '{bad', '\ufeff{}']:
            path.write_text(content)
            with self.subTest(content=content), self.assertRaises(ForgeError):
                load_json(path)

    def test_atomic_json_preserves_existing_on_invalid_value(self):
        path = self.root / "saved.json"
        path.write_text("existing bytes")
        for invalid in [{"value": float("nan")}, {"object": object()}]:
            with self.subTest(value=invalid), self.assertRaises((ValueError, TypeError)):
                atomic_json(path, invalid)
            self.assertEqual(path.read_text(), "existing bytes")
            self.assertEqual(list(self.root.glob(".forge-write-*")), [])
        atomic_json(path, {"message": "£1", "count": 1})
        self.assertEqual(load_json(path), {"message": "£1", "count": 1})

    def test_atomic_json_rejects_destination_symlink(self):
        actual = self.root / "actual"
        actual.write_text("keep")
        link = self.root / "link"
        link.symlink_to(actual)
        with self.assertRaises(ForgeError):
            atomic_json(link, {"replace": True})
        self.assertEqual(actual.read_text(), "keep")

    def test_engine_pin_and_config_schema(self):
        path = self.project / "forge.json"
        valid = {"schema": 1, "engine_version": __version__}
        atomic_json(path, valid)
        self.assertEqual(project_config(self.project), valid)
        for invalid in [{**valid, "engine_version": "0.0.0"},
                        {**valid, "schema": True}, {**valid, "schema": 999}]:
            atomic_json(path, invalid)
            with self.subTest(value=invalid), self.assertRaises(ForgeError):
                project_config(self.project)

    def test_protected_evidence_external_and_changes_detected(self):
        plan_path = self.root / "plan.json"
        evaluator = self.root / "evaluate.py"
        atomic_json(plan_path, plan())
        evaluator.write_text("assert True")
        snapshot = protect(plan_path, {**plan(), "protected_files": [str(evaluator)]}, self.project)
        verify_protected(snapshot)
        evaluator.write_text("assert False")
        with self.assertRaises(ForgeError):
            verify_protected(snapshot)
        internal = self.project / "internal.json"
        atomic_json(internal, plan())
        with self.assertRaises(ForgeError):
            protect(internal, plan(), self.project)

    def test_sqlite_reopen_reuse_and_fingerprint_separation(self):
        path = self.root / "state.sqlite"
        state = State(path)
        try:
            state.complete("run-a", "build", {"output.txt": "hash-a"})
            state.event("task_completed", {"id": "build"})
        finally:
            state.close()
        reopened = State(path)
        try:
            self.assertTrue(reopened.reusable("run-a", "build", {"output.txt": "hash-a"}))
            self.assertFalse(reopened.reusable("run-b", "build", {"output.txt": "hash-a"}))
            self.assertFalse(reopened.reusable("run-a", "build", {"output.txt": "hash-b"}))
            row = reopened.db.execute("SELECT kind,detail FROM events").fetchone()
            self.assertEqual(row, ("task_completed", canonical({"id": "build"})))
        finally:
            reopened.close()

    def test_unknown_state_schema_preserved(self):
        path = self.root / "future.sqlite"
        with closing(sqlite3.connect(path)) as connection:
            with connection:
                connection.execute("CREATE TABLE future(value TEXT)")
                connection.execute("INSERT INTO future VALUES('important')")
                connection.execute("PRAGMA user_version=99")
        before = path.read_bytes()
        with self.assertRaises(ForgeError):
            State(path)
        self.assertEqual(path.read_bytes(), before)
        with closing(sqlite3.connect(path)) as connection:
            self.assertEqual(connection.execute("SELECT value FROM future").fetchone()[0], "important")
            self.assertEqual(connection.execute("PRAGMA user_version").fetchone()[0], 99)

    def test_symlink_database_refused(self):
        actual = self.root / "actual.sqlite"
        actual.touch()
        link = self.root / "alias.sqlite"
        link.symlink_to(actual)
        with self.assertRaises(ForgeError):
            State(link)


@unittest.skipUnless(os.name == "posix", "flock contract is POSIX-specific")
class ProcessLockTests(ProjectFixture):
    def child(self, code, project):
        environment = os.environ.copy()
        environment["PYTHONPATH"] = ENGINE
        return subprocess.Popen([sys.executable, "-c", code, str(project)],
                                env=environment, stdout=subprocess.PIPE,
                                stderr=subprocess.PIPE, text=True)

    def test_competing_process_rejected_while_owner_alive(self):
        code = """
import sys
from forge.core import ForgeError, project_lock
try:
    with project_lock(sys.argv[1]):
        print('unexpected-acquisition')
except ForgeError:
    print('blocked')
"""
        with project_lock(self.project):
            child = self.child(code, self.project)
            stdout, stderr = child.communicate(timeout=5)
            self.assertEqual(child.returncode, 0, stderr)
            self.assertEqual(stdout.strip(), "blocked")
        with project_lock(self.project):
            pass

    def test_real_process_kill_releases_lock(self):
        code = """
import sys, time
from forge.core import project_lock
with project_lock(sys.argv[1]):
    print('owned', flush=True)
    time.sleep(30)
"""
        child = self.child(code, self.project)
        try:
            ready, _, _ = select.select([child.stdout], [], [], 5)
            self.assertTrue(ready, "Lock-owning child failed to report readiness")
            self.assertEqual(child.stdout.readline().strip(), "owned")
            with self.assertRaises(ForgeError):
                with project_lock(self.project):
                    pass
            child.kill()
            child.communicate(timeout=5)
            with project_lock(self.project):
                pass
        finally:
            if child.poll() is None:
                child.kill()
            child.communicate(timeout=5)

    def test_different_projects_have_independent_ownership(self):
        other = self.root / "other"
        other.mkdir()
        code = """
import sys
from forge.core import project_lock
with project_lock(sys.argv[1]):
    print('independent')
"""
        with project_lock(self.project):
            child = self.child(code, other)
            stdout, stderr = child.communicate(timeout=5)
            self.assertEqual(child.returncode, 0, stderr)
            self.assertEqual(stdout.strip(), "independent")

    def test_symlink_state_directory_and_lock_refused(self):
        outside = self.root / "outside"
        outside.mkdir()
        state = self.project / ".forge"
        state.symlink_to(outside, target_is_directory=True)
        with self.assertRaises(ForgeError):
            with project_lock(self.project):
                pass
        state.unlink()
        state.mkdir()
        (state / "lock").symlink_to(outside / "lock")
        with self.assertRaises(ForgeError):
            with project_lock(self.project):
                pass


if __name__ == "__main__":
    unittest.main()
