"""Synthetic fixtures exercise controls, not evidence of real model learning."""
from copy import deepcopy
import hashlib
import json
from pathlib import Path
import tempfile
import unittest

from forge.core import ForgeError
from forge.learning import active_lessons, export_sft, train


class LearningTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name).resolve()
        self.dataset = self.root / "episodes.json"
        self.output = self.root / "playbook.json"
        self.evidence = self.root / "observations.txt"
        self.evidence.write_text("Synthetic test fixture, not an actual agent build.")
        self.rows = [self.episode("train-1", "train"), self.episode("held-1", "holdout"),
                     self.episode("held-2", "holdout")]

    def episode(self, task_id, split, **changes):
        # origin='real' below is a test double exercising the real-origin gate.
        # It must not be reported as a real learning experiment.
        return {"task_id": task_id, "split": split, "scope": "money-validation",
                "lesson": "Reject fractional pence before calculating totals.",
                "baseline_pass": False, "candidate_pass": True,
                "evidence": [{"path": str(self.evidence),
                              "sha256": hashlib.sha256(self.evidence.read_bytes()).hexdigest()}],
                "consent": True, "origin": "real", **changes}

    def write(self, rows=None):
        self.dataset.write_text(json.dumps({"schema": 1, "episodes": self.rows if rows is None else rows}))

    def execute(self):
        self.write()
        return train(self.dataset, self.output, "money-validation")

    def test_real_gate_fixture_active_and_reproducible(self):
        result = self.execute()
        lesson = result["lessons"][0]
        self.assertEqual(lesson["status"], "active")
        self.assertEqual(lesson["support"]["holdout_real_wins"], 2)
        self.assertEqual(active_lessons(self.output, "money-validation"), [lesson["text"]])
        before = self.output.read_bytes()
        self.assertEqual(train(self.dataset, self.output, "money-validation"), result)
        self.assertEqual(self.output.read_bytes(), before)
        self.assertEqual(result["kind"], "agent_playbook_not_model_weights")

    def test_insufficient_holdout_is_candidate(self):
        self.rows.pop()
        result = self.execute()
        self.assertEqual(result["lessons"][0]["status"], "candidate")
        self.assertEqual(active_lessons(self.output, "money-validation"), [])

    def test_synthetic_examples_cannot_promote(self):
        for row in self.rows:
            row["origin"] = "synthetic"
        self.assertEqual(self.execute()["lessons"][0]["status"], "inconclusive")

    def test_synthetic_holdouts_do_not_count_as_real_wins(self):
        self.rows[1]["origin"] = self.rows[2]["origin"] = "synthetic"
        self.assertEqual(self.execute()["lessons"][0]["status"], "candidate")

    def test_any_holdout_regression_blocks_promotion(self):
        self.rows.append(self.episode("held-regression", "holdout", baseline_pass=True, candidate_pass=False))
        self.assertEqual(self.execute()["lessons"][0]["status"], "inconclusive")

    def test_no_failure_to_repair_train_pair_is_inconclusive(self):
        self.rows[0]["baseline_pass"] = True
        self.assertEqual(self.execute()["lessons"][0]["status"], "inconclusive")

    def test_different_lessons_do_not_pool_support(self):
        self.rows[2]["lesson"] = "Different rule."
        result = self.execute()
        self.assertNotIn("active", [lesson["status"] for lesson in result["lessons"]])

    def test_overlap_rejected_even_with_different_lesson(self):
        self.rows[1]["task_id"] = self.rows[0]["task_id"]
        self.rows[1]["lesson"] = "Other lesson."
        with self.assertRaises(ForgeError):
            self.execute()

    def test_duplicate_episode_rejected(self):
        self.rows.append(deepcopy(self.rows[0]))
        with self.assertRaises(ForgeError):
            self.execute()

    def test_scope_isolation(self):
        result = self.execute()
        with self.assertRaises(ForgeError):
            active_lessons(self.output, "other-scope")
        self.write()
        with self.assertRaises(ForgeError):
            train(self.dataset, self.output, "other-scope")
        self.assertEqual(result["scope"], "money-validation")

    def test_nonboolean_outcomes_and_missing_consent_rejected(self):
        for field, value in [("baseline_pass", 0), ("candidate_pass", 1), ("consent", 1),
                             ("consent", False), ("origin", "observed"), ("split", "test")]:
            rows = deepcopy(self.rows)
            rows[0][field] = value
            self.write(rows)
            with self.subTest(field=field, value=value), self.assertRaises(ForgeError):
                train(self.dataset, self.output, "money-validation")

    def test_missing_and_changed_evidence_rejected(self):
        for evidence in [[], [{"path": str(self.root / "missing"), "sha256": "0" * 64}],
                         [{"path": str(self.evidence), "sha256": "0" * 64}],
                         [{"path": "relative.txt", "sha256": "0" * 64}]]:
            rows = deepcopy(self.rows)
            rows[0]["evidence"] = evidence
            self.write(rows)
            with self.subTest(evidence=evidence), self.assertRaises(ForgeError):
                train(self.dataset, self.output, "money-validation")

    def test_evidence_edit_invalidates_active_playbook(self):
        self.execute()
        self.evidence.write_text("Changed after promotion")
        with self.assertRaises(ForgeError):
            active_lessons(self.output, "money-validation")

    def test_dataset_edit_and_playbook_status_tampering_rejected(self):
        self.execute()
        self.rows.pop()
        self.write()
        with self.assertRaises(ForgeError):
            active_lessons(self.output, "money-validation")
        candidate = train(self.dataset, self.output, "money-validation")
        candidate["lessons"][0]["status"] = "active"
        self.output.write_text(json.dumps(candidate))
        with self.assertRaises(ForgeError):
            active_lessons(self.output, "money-validation")

    def test_output_cannot_overwrite_dataset_or_any_scope_evidence(self):
        self.write()
        before = self.dataset.read_bytes()
        with self.assertRaises(ForgeError):
            train(self.dataset, self.dataset, "money-validation")
        self.assertEqual(self.dataset.read_bytes(), before)
        other = self.root / "other-scope-evidence.txt"
        other.write_text("Evidence for a different scope")
        self.rows.append(self.episode("other", "train", scope="another",
                                     evidence=[{"path": str(other), "sha256": hashlib.sha256(other.read_bytes()).hexdigest()}]))
        self.write()
        with self.assertRaises(ForgeError):
            train(self.dataset, other, "money-validation")
        alias = self.root / "hardlink"
        import os
        os.link(self.dataset, alias)
        with self.assertRaises(ForgeError):
            train(self.dataset, alias, "money-validation")

    def test_failed_training_preserves_existing_output(self):
        self.output.write_text("existing")
        self.rows[0]["consent"] = False
        with self.assertRaises(ForgeError):
            self.execute()
        self.assertEqual(self.output.read_text(), "existing")

    def test_secret_patterns_rejected_in_lesson_and_evidence(self):
        self.rows[0]["lesson"] = "Use token " + "sk-" + "A" * 30
        with self.assertRaises(ForgeError):
            self.execute()
        self.rows[0]["lesson"] = "Safe lesson"
        # Place it after 2 MiB and across a read boundary to exercise streaming scan.
        self.evidence.write_bytes(b"x" * (3 * 1024 * 1024 - 5) + b" sk-" + b"A" * 30)
        for row in self.rows:
            row["evidence"][0]["sha256"] = hashlib.sha256(self.evidence.read_bytes()).hexdigest()
        with self.assertRaises(ForgeError):
            self.execute()

    def test_external_provenance_requires_source_and_licence(self):
        self.rows[0]["provenance"] = {"source": "Reviewed public example", "url": "https://example.org", "licence": "unknown"}
        with self.assertRaises(ForgeError):
            self.execute()
        self.rows[0]["provenance"]["licence"] = "CC0; rights reviewed"
        self.assertEqual(self.execute()["lessons"][0]["status"], "active")

    def test_sft_export_refuses_fabricated_messages_and_creates_nothing(self):
        self.write()
        with self.assertRaises(ForgeError):
            export_sft(self.dataset, self.output)
        self.assertFalse(self.output.exists())

    def test_unknown_fields_and_duplicate_json_rejected(self):
        self.rows[0]["unreviewed"] = True
        with self.assertRaises(ForgeError):
            self.execute()
        self.dataset.write_text('{"schema":1,"schema":1,"episodes":[]}')
        with self.assertRaises(ForgeError):
            train(self.dataset, self.output, "money-validation")

    def test_symlink_evidence_rejected(self):
        link = self.root / "linked.txt"
        link.symlink_to(self.evidence)
        self.rows[0]["evidence"][0]["path"] = str(link)
        with self.assertRaises(ForgeError):
            self.execute()


if __name__ == "__main__":
    unittest.main()
