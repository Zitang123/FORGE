"""Real file/thread/subprocess bridge transport tests, with host doubles.

No test starts an LLM. These tests establish transport behavior only.
"""
from concurrent.futures import ThreadPoolExecutor
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import unittest

from forge.bridge import HandoffAdapter, reply
from forge.core import ForgeError, atomic_json, digest


class BridgeTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name).resolve()
        self.project = self.base / "project"
        self.project.mkdir()
        self.bridge = self.base / "bridge"
        self.adapter = HandoffAdapter(self.bridge, "explicit-host-transport-double")

    def request_record(self, request_id="a" * 32):
        prompt = "Write a local checked artifact."
        request = {"schema": 1, "id": request_id, "prompt": prompt,
                   "prompt_sha256": digest(prompt.encode()),
                   "deadline_epoch": time.time() + 10}
        path = self.bridge / (request_id + ".request.json")
        atomic_json(path, request)
        return path, request

    def exchange(self, host_action, timeout=1, max_bytes=4096):
        with ThreadPoolExecutor(max_workers=1) as executor:
            future = executor.submit(self.adapter.build, self.project, "A precise prompt", timeout, max_bytes)
            deadline = time.monotonic() + 2
            request_path = None
            while request_path is None and time.monotonic() < deadline:
                requests = list(self.bridge.glob("*.request.json"))
                if requests:
                    request_path = requests[0]
                    break
                if future.done():
                    future.result()
                time.sleep(0.005)
            self.assertIsNotNone(request_path, "request not written")
            request = json.loads(request_path.read_text())
            host_action(request_path, request)
            return future.result(timeout=timeout + 1)

    def test_no_host_response_times_out_bounded(self):
        result = self.adapter.build(self.project, "No host exists in this test", 0.04, 4096)
        self.assertEqual(result["reason"], "timeout")
        self.assertFalse(result["agent_completed"])
        self.assertIsNone(result["returncode"])
        self.assertLess(result["seconds"], 0.5)
        self.assertEqual(result["bytes"], 0)

    def test_valid_thread_host_response_is_bound_to_request_and_prompt(self):
        seen = {}
        def host(path, request):
            seen.update(request)
            reply(self.bridge, request["id"], True, "Explicit transport double, no model called")
        result = self.exchange(host)
        self.assertEqual(result["host_request"], seen["id"])
        self.assertEqual(seen["prompt_sha256"], digest(b"A precise prompt"))
        self.assertEqual(result["reason"], "completed")
        self.assertTrue(result["agent_completed"])
        self.assertEqual(result["returncode"], 0)
        self.assertIn("no model", result["host_evidence"])

    def test_real_subprocess_cli_reply(self):
        engine = Path(__file__).resolve().parents[1]
        def host(path, request):
            environment = dict(os.environ)
            environment["PYTHONPATH"] = str(engine)
            completed = subprocess.run(
                [sys.executable, "-m", "forge", "bridge-reply", "--bridge", str(self.bridge),
                 "--id", request["id"], "--completed", "--evidence", "Explicit CLI transport double"],
                capture_output=True, text=True, timeout=5, env=environment,
            )
            self.assertEqual(completed.returncode, 0, completed.stderr)
        result = self.exchange(host, timeout=5)
        self.assertTrue(result["agent_completed"])

    def test_negative_host_outcome_is_not_success(self):
        result = self.exchange(lambda path, request: reply(self.bridge, request["id"], False, "Host failed in explicit test double"))
        self.assertFalse(result["agent_completed"])
        self.assertEqual(result["returncode"], 1)

    def test_mismatched_and_malformed_response_rejected(self):
        for mutation in ({"id": "b" * 32}, {"prompt_sha256": "0" * 64}, {"completed": "true"}, {"schema": 2}, {"schema": True}):
            with self.subTest(mutation=mutation):
                # Fresh bridge avoids selecting a request from a prior exchange.
                self.bridge = self.base / ("bridge-" + str(time.monotonic_ns()))
                self.adapter = HandoffAdapter(self.bridge, "transport-double")
                def host(path, request):
                    response = {"schema": 1, "id": request["id"],
                                "prompt_sha256": request["prompt_sha256"], "completed": True,
                                "host_evidence": "Explicit transport double"}
                    response.update(mutation)
                    atomic_json(self.bridge / (request["id"] + ".response.json"), response)
                with self.assertRaises(ForgeError):
                    self.exchange(host)

    def test_invalid_json_response_rejected(self):
        def host(path, request):
            response_path = self.bridge / (request["id"] + ".response.json")
            temporary = self.bridge / "invalid-response.tmp"
            temporary.write_text("{bad json")
            temporary.replace(response_path)
        with self.assertRaises(ForgeError):
            self.exchange(host)

    def test_response_cap(self):
        with self.assertRaisesRegex(ForgeError, "cap"):
            self.exchange(lambda path, request: reply(self.bridge, request["id"], True, "x" * 1000), max_bytes=128)

    def test_late_reply_rejected(self):
        path, request = self.request_record()
        request["deadline_epoch"] = time.time() - 1
        atomic_json(path, request)
        with self.assertRaisesRegex(ForgeError, "expired"):
            reply(self.bridge, request["id"], True, "Late transport double")
        self.assertFalse((self.bridge / (request["id"] + ".response.json")).exists())

    def test_reply_does_not_overwrite_existing_evidence(self):
        _, request = self.request_record()
        reply(self.bridge, request["id"], False, "Original transport evidence")
        response = self.bridge / (request["id"] + ".response.json")
        before = response.read_bytes()
        with self.assertRaisesRegex(ForgeError, "exists"):
            reply(self.bridge, request["id"], True, "Replacement evidence")
        self.assertEqual(response.read_bytes(), before)

    def test_bridge_inside_project_rejected(self):
        adapter = HandoffAdapter(self.project / "internal-bridge", "transport-double")
        with self.assertRaisesRegex(ForgeError, "outside"):
            adapter.build(self.project, "No builder should access bridge", 0.1, 4096)

    def test_changed_request_prompt_rejected_by_reply(self):
        path, request = self.request_record()
        request["prompt"] = "Changed after the request was written"
        atomic_json(path, request)
        with self.assertRaisesRegex(ForgeError, "changed"):
            reply(self.bridge, request["id"], True, "Transport double")

    def test_direct_reply_requires_boolean_completion(self):
        _, request = self.request_record()
        with self.assertRaises(ForgeError):
            reply(self.bridge, request["id"], "false", "Transport double")

    def test_malformed_deadline_rejected_as_forge_error(self):
        path, request = self.request_record()
        request["deadline_epoch"] = "tomorrow"
        atomic_json(path, request)
        with self.assertRaises(ForgeError):
            reply(self.bridge, request["id"], True, "Transport double")


if __name__ == "__main__":
    unittest.main()
