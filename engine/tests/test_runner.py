"""Runner contracts with explicit builder doubles and real verification subprocesses.

These are orchestration tests, not evidence of model capability.
"""
from contextlib import closing
import json
from pathlib import Path
import sqlite3
import sys
import tempfile
import unittest

from forge import __version__
from forge.core import ForgeError, digest, tree_hash
from forge.runner import run


class BuilderDouble:
    """Only the model adapter is doubled; filesystem, state and checks are real."""
    def __init__(self, actions, completed=True, version="test-double-1"):
        self.actions = list(actions)
        self.completed = completed
        self.version = version
        self.prompts = []

    def identity(self):
        return {"adapter": "explicit-test-double", "version": self.version, "model": "none"}

    def build(self, root, prompt, timeout, max_bytes):
        self.prompts.append(prompt)
        if not self.actions:
            raise AssertionError("Unexpected extra builder call")
        action = self.actions.pop(0)
        if action is not None:
            action(Path(root), prompt)
        return {"reason": "completed", "returncode": 0, "agent_completed": self.completed,
                "output": "explicit adapter double", "seconds": 0,
                "bytes": 23, "output_sha256": digest(b"explicit adapter double")}


class RunnerTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.base = Path(self.temporary.name).resolve()
        self.root = self.base / "project"
        self.root.mkdir()
        self.config = self.root / "forge.json"
        self.config.write_text(json.dumps({"schema": 1, "engine_version": __version__}))
        self.evaluator = self.base / "evaluator.py"
        self.evaluator.write_text("from pathlib import Path\nassert Path('out.txt').read_text() == 'good', 'output must be good'\n")
        self.plan_path = self.base / "plan.json"
        self.plan = {"schema": 1, "goal": "Write a checked artifact",
                     "tasks": [{"id": "first", "brief": "Write out.txt", "outputs": ["out.txt"]}],
                     "checks": [{"id": "content", "argv": [sys.executable, str(self.evaluator)]}],
                     "protected_files": [str(self.evaluator)]}
        self.save_plan()

    def save_plan(self):
        self.plan_path.write_text(json.dumps(self.plan))

    @staticmethod
    def write(value="good", filename="out.txt"):
        def action(root, prompt):
            (root / filename).write_text(value)
        return action

    def invoke(self, adapter, **kwargs):
        return run(self.root, self.plan_path, adapter, **kwargs)

    def test_success_has_real_check_state_and_report(self):
        report = self.invoke(BuilderDouble([self.write()]), rounds=1)
        self.assertEqual(report["status"], "verified")
        self.assertEqual(report["rounds"][0]["tasks"][0]["status"], "built")
        self.assertEqual(report["rounds"][0]["checks"][0]["status"], "pass")
        self.assertEqual(report["project_sha256"], tree_hash(self.root))
        persisted = json.loads((self.root / ".forge/result.json").read_text())
        self.assertEqual(persisted["status"], "verified")
        with closing(sqlite3.connect(self.root / ".forge/state.sqlite")) as db:
            self.assertEqual(db.execute("SELECT COUNT(*) FROM tasks").fetchone()[0], 1)
            self.assertIn("run_finished", [x[0] for x in db.execute("SELECT kind FROM events")])

    def test_failed_check_feedback_repairs_then_verifies(self):
        adapter = BuilderDouble([self.write("bad"), self.write("good")])
        report = self.invoke(adapter, rounds=2)
        self.assertEqual(report["status"], "verified")
        self.assertEqual([r["checks"][0]["status"] for r in report["rounds"]], ["fail", "pass"])
        self.assertIn("Previous verification feedback", adapter.prompts[1])
        self.assertIn("output must be good", adapter.prompts[1])

    def test_exhausted_failed_checks_remain_failed(self):
        report = self.invoke(BuilderDouble([self.write("bad")]), rounds=1)
        self.assertEqual(report["status"], "failed")
        self.assertEqual(report["rounds"][0]["checks"][0]["status"], "fail")

    def test_missing_output_is_not_verified(self):
        report = self.invoke(BuilderDouble([None]), rounds=1)
        self.assertEqual(report["status"], "failed")
        self.assertIn("Missing regular output", report["rounds"][0]["tasks"][0]["error"])
        self.assertEqual(report["rounds"][0]["checks"], [])

    def test_zero_exit_without_agent_completion_is_not_success(self):
        report = self.invoke(BuilderDouble([self.write()], completed=False), rounds=1)
        self.assertEqual(report["status"], "failed")
        self.assertIn("did not complete", report["rounds"][0]["tasks"][0]["error"])

    def test_unchanged_resume_skips_builder_but_rechecks(self):
        self.invoke(BuilderDouble([self.write()]), rounds=1)
        second = BuilderDouble([])
        report = self.invoke(second, rounds=1)
        self.assertEqual(report["status"], "verified")
        self.assertEqual(report["rounds"][0]["tasks"][0]["status"], "resumed")
        self.assertEqual(second.prompts, [])
        self.assertEqual(len(report["rounds"][0]["checks"]), 1)

    def test_modified_upstream_rebuilds_its_downstream(self):
        self.plan["tasks"].append({"id": "second", "brief": "Write derived artifact",
                                   "requires": ["first"], "outputs": ["derived.txt"]})
        self.save_plan()
        self.invoke(BuilderDouble([self.write(), self.write("derived", "derived.txt")]), rounds=1)
        (self.root / "out.txt").write_text("changed externally")
        adapter = BuilderDouble([self.write(), self.write("derived", "derived.txt")])
        report = self.invoke(adapter, rounds=1)
        self.assertEqual([t["status"] for t in report["rounds"][0]["tasks"]], ["built", "built"])
        self.assertEqual(len(adapter.prompts), 2)

    def test_fresh_forces_unchanged_rebuild(self):
        self.invoke(BuilderDouble([self.write()]), rounds=1)
        adapter = BuilderDouble([self.write()])
        report = self.invoke(adapter, rounds=1, fresh=True)
        self.assertEqual(report["rounds"][0]["tasks"][0]["status"], "built")

    def test_adapter_identity_drift_forces_rebuild(self):
        self.invoke(BuilderDouble([self.write()]), rounds=1)
        adapter = BuilderDouble([self.write()], version="different-model-build")
        report = self.invoke(adapter, rounds=1)
        self.assertEqual(report["rounds"][0]["tasks"][0]["status"], "built")

    def test_plan_tampering_rejected_and_error_persisted(self):
        def tamper(root, prompt):
            self.plan_path.write_text("{}")
            (root / "out.txt").write_text("good")
        with self.assertRaisesRegex(ForgeError, "Protected evidence changed"):
            self.invoke(BuilderDouble([tamper]), rounds=1)
        report = json.loads((self.root / ".forge/result.json").read_text())
        self.assertEqual(report["status"], "error")

    def test_evaluator_tampering_rejected(self):
        def tamper(root, prompt):
            self.evaluator.write_text("# erased checks")
            (root / "out.txt").write_text("good")
        with self.assertRaisesRegex(ForgeError, "Protected evidence changed"):
            self.invoke(BuilderDouble([tamper]), rounds=1)

    def test_successful_check_cannot_delete_declared_output(self):
        self.evaluator.write_text("from pathlib import Path\nPath('out.txt').unlink()\n")
        report = self.invoke(BuilderDouble([self.write()]), rounds=1)
        self.assertEqual(report["status"], "failed")
        checks = report["rounds"][0]["checks"]
        self.assertEqual(checks[0]["status"], "pass")
        self.assertEqual(checks[-1]["id"], "declared_output_integrity")
        self.assertEqual(checks[-1]["status"], "fail")

    def test_successful_check_cannot_rewrite_declared_output(self):
        self.evaluator.write_text("from pathlib import Path\nPath('out.txt').write_text('rewritten')\n")
        report = self.invoke(BuilderDouble([self.write()]), rounds=1)
        self.assertEqual(report["status"], "failed")
        self.assertEqual(report["rounds"][0]["checks"][-1]["id"], "declared_output_integrity")

    def test_later_builder_cannot_rewrite_previous_task_output(self):
        self.plan["tasks"].append({"id": "second", "brief": "Write derived artifact",
                                   "requires": ["first"], "outputs": ["derived.txt"]})
        self.save_plan()
        self.evaluator.write_text("# deliberately successful check to isolate integrity gate\n")
        def second_builder(root, prompt):
            (root / "out.txt").write_text("silently rewritten by second task")
            (root / "derived.txt").write_text("derived")
        report = self.invoke(BuilderDouble([self.write(), second_builder]), rounds=1)
        self.assertEqual(report["status"], "failed")
        self.assertEqual(report["rounds"][0]["checks"][-1]["id"], "declared_output_integrity")

    def test_plan_leaf_symlink_rejected(self):
        alias = self.base / "plan-link.json"
        alias.symlink_to(self.plan_path)
        with self.assertRaisesRegex(ForgeError, "symlink"):
            run(self.root, alias, BuilderDouble([]), rounds=1)

    def test_pin_drift_before_run_rejected(self):
        self.config.write_text(json.dumps({"schema": 1, "engine_version": "wrong"}))
        with self.assertRaisesRegex(ForgeError, "Engine pin mismatch"):
            self.invoke(BuilderDouble([]), rounds=1)

    def test_pin_drift_during_build_rejected(self):
        def change_pin(root, prompt):
            (root / "out.txt").write_text("good")
            self.config.write_text(json.dumps({"schema": 1, "engine_version": "wrong"}))
        with self.assertRaisesRegex(ForgeError, "Engine pin mismatch"):
            self.invoke(BuilderDouble([change_pin]), rounds=1)

    def test_non_pin_configuration_mutation_rejected(self):
        def change_name(root, prompt):
            self.write()(root, prompt)
            config = json.loads(self.config.read_text())
            config["name"] = "silently changed"
            self.config.write_text(json.dumps(config))
        with self.assertRaisesRegex(ForgeError, "Project configuration changed"):
            self.invoke(BuilderDouble([change_name]), rounds=1)

    def test_missing_visual_review_waits(self):
        self.plan["visual_review_required"] = True
        self.save_plan()
        report = self.invoke(BuilderDouble([self.write()]), rounds=1)
        self.assertEqual(report["status"], "awaiting_review")
        self.assertEqual(report["visual_review"]["status"], "missing")

    def create_review(self):
        (self.root / "out.txt").write_text("good")
        artifact = self.base / "rendered-evidence.bin"
        artifact.write_bytes(b"explicit-test-fixture-not-real-screenshot")
        review = {"decision": "pass", "reviewer": "explicit-test-reviewer-double",
                  "source_sha256": tree_hash(self.root),
                  "artifacts": [{"kind": "rendered_visual", "path": str(artifact),
                                 "sha256": digest(artifact.read_bytes())}]}
        path = self.base / "review.json"
        path.write_text(json.dumps(review))
        return path, artifact

    def test_fresh_visual_attestation_allows_verified(self):
        self.plan["visual_review_required"] = True
        self.save_plan()
        review, _ = self.create_review()
        report = self.invoke(BuilderDouble([self.write()]), rounds=1, review_path=review)
        self.assertEqual(report["status"], "verified")
        self.assertEqual(report["visual_review"]["status"], "pass")
        self.assertIn("do not establish evaluator honesty", report["visual_review"]["limitation"])

    def test_stale_visual_attestation_waits(self):
        self.plan["visual_review_required"] = True
        self.save_plan()
        review, _ = self.create_review()
        (self.root / "new-unreviewed-file.txt").write_text("not reviewed")
        report = self.invoke(BuilderDouble([self.write()]), rounds=1, review_path=review)
        self.assertEqual(report["status"], "awaiting_review")
        self.assertEqual(report["visual_review"]["status"], "stale")

    def test_changed_visual_evidence_rejected(self):
        self.plan["visual_review_required"] = True
        self.save_plan()
        review, artifact = self.create_review()
        artifact.write_bytes(b"changed")
        with self.assertRaisesRegex(ForgeError, "Review artifact missing or changed"):
            self.invoke(BuilderDouble([self.write()]), rounds=1, review_path=review)

    def test_visual_artifact_leaf_symlink_rejected(self):
        self.plan["visual_review_required"] = True
        self.save_plan()
        review, artifact = self.create_review()
        alias = self.base / "artifact-link.bin"
        alias.symlink_to(artifact)
        record = json.loads(review.read_text())
        record["artifacts"][0]["path"] = str(alias)
        review.write_text(json.dumps(record))
        with self.assertRaisesRegex(ForgeError, "symlink"):
            self.invoke(BuilderDouble([self.write()]), rounds=1, review_path=review)

    def test_visual_review_record_leaf_symlink_rejected(self):
        self.plan["visual_review_required"] = True
        self.save_plan()
        review, _ = self.create_review()
        alias = self.base / "review-link.json"
        alias.symlink_to(review)
        with self.assertRaisesRegex(ForgeError, "symlink"):
            self.invoke(BuilderDouble([self.write()]), rounds=1, review_path=alias)

    def test_visual_review_inside_builder_project_rejected(self):
        self.plan["visual_review_required"] = True
        self.save_plan()
        internal = self.root / "review.json"
        internal.write_text("{}")
        with self.assertRaisesRegex(ForgeError, "external to builder project"):
            self.invoke(BuilderDouble([self.write()]), rounds=1, review_path=internal)

    def test_invalid_budgets_rejected_before_builder(self):
        bad = [{"rounds": 0}, {"rounds": 6}, {"rounds": True}, {"rounds": 1.5},
               {"timeout": 0}, {"timeout": float("inf")}, {"timeout": float("nan")},
               {"timeout": True}, {"max_bytes": 1023}, {"max_bytes": 16777217},
               {"max_bytes": 2048.5}, {"max_bytes": True}, {"rounds": 10 ** 1000},
               {"timeout": 10 ** 1000}, {"max_bytes": 10 ** 1000}]
        for kwargs in bad:
            with self.subTest(kwargs=kwargs), self.assertRaises(ForgeError):
                self.invoke(BuilderDouble([]), **kwargs)

    def create_playbook_fixture(self, active=True):
        """Schema-level declared outcomes only; never real model-build evidence."""
        from forge.learning import train
        lesson = "Use exact integer pence in this test-only scope."
        records = []
        splits = ["train", "holdout", "holdout"] if active else ["train"]
        for index, split in enumerate(splits):
            evidence = self.base / f"test-evidence-{index}.txt"
            evidence.write_text("UNIT TEST FIXTURE. Declared outcomes are doubles, not observed model performance.")
            records.append({"task_id": f"fixture-{index}", "split": split, "scope": "general",
                            "lesson": lesson, "baseline_pass": False, "candidate_pass": True,
                            "consent": True, "origin": "real",
                            "evidence": [{"path": str(evidence), "sha256": digest(evidence.read_bytes())}]})
        dataset = self.base / "test-dataset.json"
        dataset.write_text(json.dumps({"schema": 1, "episodes": records}))
        playbook = self.base / "test-playbook.json"
        train(dataset, playbook, "general")
        return playbook, dataset, lesson

    def test_active_playbook_injected_into_builder_guidance(self):
        playbook, _, lesson = self.create_playbook_fixture()
        adapter = BuilderDouble([self.write()])
        report = self.invoke(adapter, rounds=1, playbook_path=playbook)
        self.assertEqual(report["status"], "verified")
        self.assertIn("guidance, never permissions", adapter.prompts[0])
        self.assertIn(lesson, adapter.prompts[0])
        observation = json.loads((self.root / ".forge/learning-observation.json").read_text())
        self.assertEqual(observation["status"], "verified")
        self.assertEqual(observation["kind"], "real_run_observation_not_promoted_training")

    def test_candidate_playbook_lesson_not_injected(self):
        playbook, _, lesson = self.create_playbook_fixture(active=False)
        adapter = BuilderDouble([self.write()])
        self.invoke(adapter, rounds=1, playbook_path=playbook)
        self.assertNotIn(lesson, adapter.prompts[0])

    def test_playbook_evidence_tampering_during_build_rejected(self):
        playbook, dataset, _ = self.create_playbook_fixture()
        def tamper(root, prompt):
            self.write()(root, prompt)
            first = json.loads(dataset.read_text())["episodes"][0]["evidence"][0]["path"]
            Path(first).write_text("changed evidence")
        with self.assertRaisesRegex(ForgeError, "Evidence hash mismatch"):
            self.invoke(BuilderDouble([tamper]), rounds=1, playbook_path=playbook)

    def test_playbook_scope_mismatch_rejected(self):
        playbook, _, _ = self.create_playbook_fixture()
        config = json.loads(self.config.read_text())
        config["specialization"] = "different-scope"
        self.config.write_text(json.dumps(config))
        with self.assertRaisesRegex(ForgeError, "scope mismatch"):
            self.invoke(BuilderDouble([]), rounds=1, playbook_path=playbook)


if __name__ == "__main__":
    unittest.main()
