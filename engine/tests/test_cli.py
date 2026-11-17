"""Real CLI/zipapp subprocess tests; no live model or network is invoked.

The zipapp fingerprint test explicitly uses a runtime builder double. Synthetic
learning records exercise validation only and cannot establish real promotion.
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
ENGINE = ROOT / "engine"


class CLITests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        built = subprocess.run([sys.executable, str(ROOT / "build.py")], cwd=ROOT,
                               capture_output=True, text=True, timeout=20)
        if built.returncode:
            raise AssertionError(built.stdout + built.stderr)
        cls.archive = ROOT / "dist/forge-3.1.0.pyz"

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="forge-cli-tests-")
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name).resolve()
        self.project = self.base / "project"
        self.env = dict(os.environ, PYTHONPATH=str(ENGINE), PYTHONDONTWRITEBYTECODE="1")

    def invoke(self, *args, archive=False, expected=0):
        env = dict(self.env)
        if archive:
            env.pop("PYTHONPATH", None)
            command = [sys.executable, str(self.archive)]
        else:
            command = [sys.executable, "-m", "forge"]
        result = subprocess.run(command + list(map(str, args)), cwd=self.base,
                                env=env, capture_output=True, text=True, timeout=30)
        self.assertEqual(result.returncode, expected, result.stdout + result.stderr)
        self.assertNotIn("Traceback", result.stderr)
        return result

    def initialize(self, archive=False):
        result = self.invoke("init", self.project, "--name", "Trial product",
                             "--brief", "Build a local checked artifact", archive=archive)
        self.assertEqual(json.loads(result.stdout)["status"], "created")

    def make_plan(self):
        plan = {"schema": 1, "goal": "Write one artifact",
                "tasks": [{"id": "artifact", "brief": "Write out.txt", "outputs": ["out.txt"]}],
                "checks": [{"id": "content", "argv": [sys.executable, "-c", "from pathlib import Path; assert Path('out.txt').read_text() == 'good'"]}]}
        path = self.base / "plan.json"
        path.write_text(json.dumps(plan))
        return path

    def test_source_version_init_doctor_intake_and_hash(self):
        self.assertEqual(self.invoke("--version").stdout.strip(), "3.1.0")
        self.initialize()
        self.assertEqual(sorted(p.relative_to(self.project).as_posix() for p in self.project.rglob("*") if p.is_file()),
                         ["AGENTS.md", "PROJECT.md", "acceptance.json", "forge.json", "memory/NOW.md"])
        config = json.loads((self.project / "forge.json").read_text())
        self.assertEqual(config["engine_version"], "3.1.0")
        doctor = json.loads(self.invoke("doctor", self.project).stdout)
        self.assertEqual(doctor["authentication"], "not_verified")
        self.assertIn("python3", doctor["tools"])
        intake = json.loads(self.invoke("intake", self.project).stdout)
        self.assertEqual(intake["known"]["name"], "Trial product")
        self.assertLessEqual(len(intake["question_groups"]), 3)
        before = json.loads(self.invoke("hash", self.project).stdout)["source_sha256"]
        self.assertEqual(len(before), 64)
        (self.project / "new.txt").write_text("changed")
        after = json.loads(self.invoke("hash", self.project).stdout)["source_sha256"]
        self.assertNotEqual(before, after)

    def test_existing_destination_is_preserved_and_rejected(self):
        self.project.mkdir()
        preserved = self.project / "user.txt"
        preserved.write_bytes(b"user changes\x00")
        result = self.invoke("init", self.project, "--name", "Trial", "--brief", "brief", expected=2)
        self.assertIn("preserved", result.stderr)
        self.assertEqual(preserved.read_bytes(), b"user changes\x00")
        self.assertEqual(list(self.project.iterdir()), [preserved])

    @unittest.skipUnless(shutil.which("git"), "Git executable unavailable")
    def test_git_init_is_idempotent_and_preserves_existing_metadata(self):
        self.initialize()
        first = json.loads(self.invoke("git-init", self.project).stdout)
        self.assertEqual(first["status"], "initialized")
        marker = self.project / ".git/forge-test-marker"
        marker.write_text("preserve metadata")
        config = (self.project / ".git/config").read_bytes()
        second = json.loads(self.invoke("git-init", self.project).stdout)
        self.assertEqual(second["status"], "preserved")
        self.assertEqual(marker.read_text(), "preserve metadata")
        self.assertEqual((self.project / ".git/config").read_bytes(), config)

    def test_plan_validation_and_json_errors_have_real_exit_status(self):
        plan = self.make_plan()
        self.assertEqual(json.loads(self.invoke("validate-plan", plan).stdout)["status"], "valid")
        plan.write_text('{"schema":1,"schema":1}')
        invalid = self.invoke("validate-plan", plan, expected=2)
        self.assertEqual(invalid.stdout, "")
        self.assertIn("Duplicate JSON key", invalid.stderr)
        plan.write_text("{")
        self.assertIn("Cannot read JSON", self.invoke("validate-plan", plan, expected=2).stderr)
        self.invoke("doctor", self.base / "missing", expected=2)

    def test_run_requires_execute_before_constructing_model_adapter(self):
        self.initialize()
        result = self.invoke("run", self.project, "--plan", self.make_plan(),
                             "--model", "never-invoked-test-model", expected=2)
        self.assertIn("Execution not requested", result.stderr)
        self.assertFalse((self.project / ".forge").exists())

    def test_train_synthetic_record_is_inconclusive_not_promotion(self):
        evidence = self.base / "synthetic-evidence.txt"
        evidence.write_text("Synthetic CLI validation fixture, not live learning evidence.\n")
        dataset = self.base / "dataset.json"
        dataset.write_text(json.dumps({"schema": 1, "episodes": [{
            "task_id": "synthetic-example", "split": "train", "scope": "cli-test",
            "lesson": "Validate before writing", "baseline_pass": False, "candidate_pass": True,
            "evidence": [{"path": str(evidence), "sha256": hashlib.sha256(evidence.read_bytes()).hexdigest()}],
            "consent": True, "origin": "synthetic"}]}))
        output = self.base / "playbook.json"
        trained = json.loads(self.invoke("train", dataset, "--scope", "cli-test", "--output", output).stdout)
        self.assertEqual(trained["kind"], "agent_playbook_not_model_weights")
        self.assertEqual(trained["lessons"][0]["status"], "inconclusive")
        self.assertEqual(json.loads(output.read_text()), trained)
        self.invoke("train", dataset, "--scope", "cli-test", "--output", dataset, expected=2)

    def test_relocatable_zipapp_without_repo_or_pythonpath(self):
        archive = self.base / "forge.pyz"
        shutil.copyfile(self.archive, archive)
        self.archive = archive
        self.assertFalse((self.base / ".git").exists())
        self.assertEqual(self.invoke("--version", archive=True).stdout.strip(), "3.1.0")
        self.initialize(archive=True)
        report = json.loads(self.invoke("doctor", self.project, archive=True).stdout)
        self.assertEqual(report["engine_version"], "3.1.0")
        self.invoke("validate-plan", self.base / "nonexistent.json", archive=True, expected=2)
        self.invoke("run", self.project, "--plan", self.make_plan(), "--model", "unused", archive=True, expected=2)

    def test_zipapp_fingerprint_binds_source_with_explicit_builder_double(self):
        self.initialize(archive=True)
        original = self.base / "original.pyz"
        changed = self.base / "changed.pyz"
        shutil.copyfile(self.archive, original)
        with zipfile.ZipFile(original) as source, zipfile.ZipFile(changed, "w", zipfile.ZIP_DEFLATED) as target:
            for name in source.namelist():
                content = source.read(name)
                if name == "forge/cli.py":
                    content += b"\n# Test-only harmless source fingerprint mutation.\n"
                target.writestr(name, content)
        script = self.base / "runtime_double.py"
        script.write_text('''import json,sys
from pathlib import Path
sys.path.insert(0,sys.argv[1])
from forge.runner import run
class BuilderDouble:
    def identity(self): return {"adapter":"explicit-test-double","version":"1","model":"none"}
    def build(self,root,prompt,timeout,max_bytes):
        (Path(root)/"out.txt").write_text("good")
        return {"reason":"completed","returncode":0,"agent_completed":True,"output":"test double"}
result=run(sys.argv[2],sys.argv[3],BuilderDouble(),rounds=1)
print(json.dumps({"status":result["status"],"fingerprint":result["fingerprint"],"task_status":result["rounds"][0]["tasks"][0]["status"]}))
''')
        plan = self.make_plan()
        reports = []
        env = dict(self.env)
        env.pop("PYTHONPATH", None)
        for archive in (original, original, changed):
            result = subprocess.run([sys.executable, str(script), str(archive), str(self.project), str(plan)],
                                    cwd=self.base, env=env, capture_output=True, text=True, timeout=20)
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            reports.append(json.loads(result.stdout))
        self.assertEqual([r["status"] for r in reports], ["verified"] * 3)
        self.assertEqual([r["task_status"] for r in reports], ["built", "resumed", "built"])
        self.assertEqual(reports[0]["fingerprint"], reports[1]["fingerprint"])
        self.assertNotEqual(reports[0]["fingerprint"], reports[2]["fingerprint"])


if __name__ == "__main__":
    unittest.main()
