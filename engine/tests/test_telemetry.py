"""Real SQLite telemetry and in-memory HTTP-handler contract tests.

No listening sockets or browser are used. In-memory request/response checks do
not establish that a live server can bind or that the frontend was rendered.
"""
import base64
from contextlib import closing
import hashlib
import io
import json
import os
from pathlib import Path
import re
import sqlite3
import subprocess
import sys
import types
import tempfile
import unittest
from unittest.mock import patch

from forge.cli import initialize
from forge.core import ForgeError, State
from forge.runner import run
from forge.telemetry import emit, handler_for, project_identity, snapshot


class MemoryConnection:
    """Socket-shaped adapter for BaseHTTPRequestHandler; opens no socket."""
    def __init__(self, request):
        self.request = io.BytesIO(request)
        self.response = bytearray()

    def makefile(self, *args, **kwargs):
        return self.request

    def sendall(self, data):
        self.response.extend(data)


class TelemetryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="forge-telemetry-tests-")
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name).resolve()
        self.project = self.base / "project"
        self.other = self.base / "other"
        initialize(self.project, "Project A", "Build project A")
        initialize(self.other, "Project B", "Build project B")

    def event(self, kind, stamp=1000, **kwargs):
        with patch("forge.core.time.time", return_value=stamp):
            return emit(self.project, kind, **kwargs)

    def engine_event(self, kind, detail, stamp=1000):
        state_path = self.project / ".forge/state.sqlite"
        state_path.parent.mkdir(exist_ok=True)
        state = State(state_path)
        try:
            with patch("forge.core.time.time", return_value=stamp):
                state.event(kind, detail)
        finally:
            state.close()

    def test_fresh_is_idle_without_fabricated_agents_or_tokens(self):
        result = snapshot(self.project, now=1000)
        self.assertEqual(result["status"], "idle")
        self.assertEqual(result["agents"], [])
        self.assertEqual(result["events"], [])
        self.assertEqual(result["stats"]["agents_started"], 0)
        self.assertEqual(result["stats"]["elapsed_seconds"], 0)
        self.assertIsNone(result["stats"]["tokens"])
        self.assertFalse((self.project / ".forge").exists())

    def test_two_real_sqlite_projects_are_isolated(self):
        self.event("session.started", agent_id="coordinator")
        emit(self.other, "agent.started", agent_id="other-agent", label="Other project")
        self.assertTrue((self.project / ".forge/state.sqlite").is_file())
        self.assertTrue((self.other / ".forge/state.sqlite").is_file())
        left, right = snapshot(self.project), snapshot(self.other)
        self.assertNotEqual(left["project"]["id"], right["project"]["id"])
        self.assertEqual([a["id"] for a in left["agents"]], ["coordinator"])
        self.assertEqual([a["id"] for a in right["agents"]], ["other-agent"])
        self.assertNotIn("other-agent", json.dumps(left))

    def test_explicit_session_progress_checks_completion_and_staleness(self):
        self.event("session.started", stamp=1000, agent_id="lead", label="Build project")
        self.event("agent.started", stamp=1001, agent_id="builder", parent_id="lead", label="Implement")
        self.event("agent.progress", stamp=1002, agent_id="builder", label="Checking outputs", tokens=12)
        self.event("check.passed", stamp=1003, agent_id="builder", label="Contract passed")
        live = snapshot(self.project, now=1004)
        self.assertEqual(live["status"], "running")
        self.assertEqual(live["stats"]["agents_started"], 1)
        self.assertEqual(live["stats"]["checks_passed"], 1)
        self.assertEqual(live["stats"]["tokens"], 12)
        builder = next(a for a in live["agents"] if a["id"] == "builder")
        self.assertEqual(builder["parent_id"], "lead")
        self.assertEqual(builder["label"], "Checking outputs")
        self.assertEqual(snapshot(self.project, now=1200)["status"], "stale")
        self.event("agent.completed", stamp=1005, agent_id="builder")
        self.event("session.completed", stamp=1006, agent_id="lead")
        done = snapshot(self.project, now=9999)
        self.assertEqual(done["status"], "idle")
        self.assertEqual(done["stats"]["elapsed_seconds"], 6)
        self.assertTrue(all(a["status"] == "completed" for a in done["agents"]))

    def test_new_explicit_session_resets_current_graph_and_counters(self):
        self.event("session.started", agent_id="old")
        self.event("agent.started", agent_id="old-child", tokens=9)
        self.event("check.failed", agent_id="old-child")
        self.event("session.completed", agent_id="old")
        self.event("session.started", stamp=1010, agent_id="new")
        result = snapshot(self.project, now=1011)
        self.assertEqual([a["id"] for a in result["agents"]], ["new"])
        self.assertEqual(result["stats"]["agents_started"], 0)
        self.assertEqual(result["stats"]["checks_failed"], 0)
        self.assertIsNone(result["stats"]["tokens"])

    def test_engine_state_events_normalize_without_raw_diagnostics(self):
        self.engine_event("agent_started", {"task": "build", "round": 1}, stamp=1000)
        self.engine_event("agent_finished", {"id": "build", "status": "built", "process": {
            "usage": {"input_tokens": 5, "output_tokens": 7}, "diagnostic": "DO_NOT_EXPOSE_SECRET"}}, stamp=1001)
        self.engine_event("check_started", {"id": "contract"}, stamp=1002)
        running = snapshot(self.project, now=1002)
        self.assertEqual(running["status"], "running")
        self.engine_event("check", {"id": "contract", "status": "pass", "process": {"diagnostic": "DO_NOT_EXPOSE_SECRET"}}, stamp=1003)
        self.engine_event("run_finished", {"status": "verified"}, stamp=1004)
        done = snapshot(self.project, now=1005)
        self.assertEqual(done["status"], "idle")
        self.assertEqual(done["stats"]["tokens"], 12)
        self.assertEqual(done["stats"]["checks_passed"], 1)
        self.assertNotIn("DO_NOT_EXPOSE_SECRET", json.dumps(done))
        self.assertTrue(all(e["detail"] == {} for e in done["events"]))

    def test_next_engine_run_replaces_prior_run_graph(self):
        self.engine_event("agent_started", {"task": "old"}, stamp=1000)
        self.engine_event("check", {"id": "old", "status": "fail"}, stamp=1001)
        self.engine_event("run_finished", {"status": "failed"}, stamp=1002)
        self.engine_event("agent_started", {"task": "new"}, stamp=1010)
        result = snapshot(self.project, now=1011)
        self.assertEqual([a.get("task_id") for a in result["agents"]], ["new"])
        self.assertEqual(result["stats"]["checks_failed"], 0)
        self.assertEqual(result["stats"]["agents_started"], 1)

    def test_resumed_real_engine_events_reset_graph_with_explicit_builder_double(self):
        class BuilderDouble:
            def __init__(self): self.calls = 0
            def identity(self): return {"adapter": "explicit-telemetry-test-double", "model": "none", "version": "1"}
            def build(self, root, prompt, timeout, max_bytes):
                self.calls += 1
                (root / "out.txt").write_text("artifact")
                return {"reason": "completed", "returncode": 0, "agent_completed": True, "output": "explicit test double"}
        plan = self.base / "plan.json"
        plan.write_text(json.dumps({"schema": 1, "goal": "Checked artifact",
            "tasks": [{"id": "artifact", "brief": "Write out.txt", "outputs": ["out.txt"]}],
            "checks": [{"id": "resumed-check", "argv": [sys.executable, "-c", "pass"]}]}))
        first = BuilderDouble()
        self.assertEqual(run(self.project, plan, first, rounds=1)["status"], "verified")
        self.assertEqual(first.calls, 1)
        second = BuilderDouble()
        self.assertEqual(run(self.project, plan, second, rounds=1)["status"], "verified")
        self.assertEqual(second.calls, 0)
        result = snapshot(self.project)
        self.assertEqual(len(result["agents"]), 1)
        self.assertEqual(result["agents"][0].get("check_id"), "resumed-check")
        self.assertEqual(result["stats"]["agents_started"], 0)

    def test_invalid_emitted_ids_types_and_tokens_are_rejected(self):
        for kwargs in [{"kind": "unknown"}, {"kind": []}, {"kind": "agent.started", "agent_id": None},
                       {"kind": "agent.started", "agent_id": "bad id"},
                       {"kind": "agent.started", "parent_id": "../parent"},
                       {"kind": "agent.started", "tokens": -1}, {"kind": "agent.started", "tokens": True},
                       {"kind": "agent.started", "tokens": 1.2}, {"kind": "agent.started", "tokens": 10**12 + 1}]:
            with self.subTest(kwargs=kwargs), self.assertRaises(ForgeError):
                emit(self.project, **kwargs)
        self.assertFalse((self.project / ".forge").exists())

    def test_corrupt_database_reports_unavailable_without_mutation(self):
        state = self.project / ".forge"
        state.mkdir()
        data = b"this is not a sqlite database"
        (state / "state.sqlite").write_bytes(data)
        result = snapshot(self.project)
        self.assertEqual(result["status"], "error")
        self.assertIn("unavailable", result["warning"])
        self.assertEqual((state / "state.sqlite").read_bytes(), data)

    def test_malformed_rows_do_not_crash_snapshot(self):
        self.engine_event("telemetry", {"type": [], "agent_id": "bad"})
        self.engine_event("agent_finished", {"id": "bad", "status": "built", "process": None})
        state_path = self.project / ".forge/state.sqlite"
        with closing(sqlite3.connect(state_path)) as db:
            with db:
                db.execute("INSERT INTO events(time,kind,detail) VALUES(?,?,?)", (1000, "telemetry", "{"))
        result = snapshot(self.project, now=1001)
        self.assertIsInstance(result, dict)
        self.assertNotIn("diagnostic", json.dumps(result))

    def test_locked_database_reports_temporarily_unavailable(self):
        self.event("idle")
        with closing(sqlite3.connect(self.project / ".forge/state.sqlite")) as db:
            db.execute("BEGIN EXCLUSIVE")
            try:
                result = snapshot(self.project)
                self.assertEqual(result["status"], "error")
                self.assertIn("unavailable", result["warning"])
            finally:
                db.rollback()

    def test_unknown_schema_refuses_without_migration(self):
        self.event("idle")
        with closing(sqlite3.connect(self.project / ".forge/state.sqlite")) as db:
            db.execute("PRAGMA user_version=99")
        with self.assertRaises(ForgeError): snapshot(self.project)
        with closing(sqlite3.connect(self.project / ".forge/state.sqlite")) as db:
            self.assertEqual(db.execute("PRAGMA user_version").fetchone()[0], 99)

    def test_symlink_state_and_database_are_rejected(self):
        target = self.base / "target"
        target.mkdir()
        (self.project / ".forge").symlink_to(target, target_is_directory=True)
        with self.assertRaises(ForgeError): snapshot(self.project)
        with self.assertRaises(ForgeError): emit(self.project, "idle")
        (self.project / ".forge").unlink()
        (self.project / ".forge").mkdir()
        db_target = target / "state.sqlite"
        db_target.write_bytes(b"preserve")
        (self.project / ".forge/state.sqlite").symlink_to(db_target)
        with self.assertRaises(ForgeError): snapshot(self.project)
        with self.assertRaises(ForgeError): emit(self.project, "idle")
        self.assertEqual(db_target.read_bytes(), b"preserve")

    def request(self, path, method="GET", host="127.0.0.1:8765", origin=None):
        html = Path(__file__).resolve().parents[1] / "forge/web/index.html"
        if not html.is_file(): self.skipTest("Observatory frontend resource not written yet")
        headers = [f"{method} {path} HTTP/1.1", f"Host: {host}", "Connection: close"]
        if origin is not None: headers.append("Origin: " + origin)
        connection = MemoryConnection(("\r\n".join(headers) + "\r\n\r\n").encode())
        handler_for(self.project, 8765)(connection, ("127.0.0.1", 12345), types.SimpleNamespace())
        head, body = bytes(connection.response).split(b"\r\n\r\n", 1)
        lines = head.decode().split("\r\n")
        status = int(lines[0].split()[1])
        parsed = dict(line.split(": ", 1) for line in lines[1:])
        return status, parsed, body

    def test_http_in_memory_redirect_namespace_and_no_arbitrary_paths(self):
        prefix = "/p/" + project_identity(self.project)["id"] + "/"
        status, headers, _ = self.request("/")
        self.assertEqual(status, 303)
        self.assertEqual(headers["Location"], prefix)
        for path in ["/p/" + project_identity(self.other)["id"] + "/api/state", "/etc/passwd", prefix + "../../PROJECT.md", prefix + "forge.json"]:
            self.assertEqual(self.request(path)[0], 404)

    def test_http_in_memory_host_origin_and_readonly_contract(self):
        prefix = "/p/" + project_identity(self.project)["id"] + "/"
        for host in ["evil.example", "127.0.0.1", "localhost:9999", "127.0.0.1:8765.evil.example"]:
            self.assertEqual(self.request(prefix, host=host)[0], 403)
        for origin in ["https://evil.example", "null", "http://localhost:9999"]:
            self.assertEqual(self.request(prefix, origin=origin)[0], 403)
        self.assertEqual(self.request(prefix, host="localhost:8765", origin="http://localhost:8765")[0], 200)
        for method in ["POST", "PUT", "PATCH", "DELETE"]:
            self.assertEqual(self.request(prefix + "api/state", method=method)[0], 405)

    def test_http_in_memory_json_html_csp_and_diagnostic_exclusion(self):
        self.engine_event("check", {"id": "check", "status": "fail", "process": {"diagnostic": "SENSITIVE_RAW_DIAGNOSTIC"}})
        prefix = "/p/" + project_identity(self.project)["id"] + "/"
        status, headers, body = self.request(prefix + "api/state")
        self.assertEqual(status, 200)
        self.assertTrue(headers["Content-Type"].startswith("application/json"))
        self.assertEqual(headers["Cache-Control"], "no-store")
        self.assertEqual(json.loads(body)["project"]["id"], project_identity(self.project)["id"])
        self.assertNotIn(b"SENSITIVE_RAW_DIAGNOSTIC", body)
        status, headers, body = self.request(prefix)
        self.assertEqual(status, 200)
        self.assertTrue(headers["Content-Type"].startswith("text/html"))
        self.assertIn("default-src 'none'", headers["Content-Security-Policy"])
        self.assertIn("frame-ancestors 'none'", headers["Content-Security-Policy"])
        for script in re.findall(rb"<script>([\s\S]*?)</script>", body):
            value = base64.b64encode(hashlib.sha256(script).digest()).decode()
            self.assertIn("'sha256-" + value + "'", headers["Content-Security-Policy"])

    def test_zipapp_contains_observatory_resource_and_constructs_handler_without_socket(self):
        root = Path(__file__).resolve().parents[2]
        built = subprocess.run([sys.executable, str(root / "build.py")], cwd=root,
                               capture_output=True, text=True, timeout=20)
        self.assertEqual(built.returncode, 0, built.stdout + built.stderr)
        archive = root / "dist/forge-3.1.0.pyz"
        script = '''import sys
sys.path.insert(0,sys.argv[1])
from importlib.resources import files
from forge.telemetry import handler_for
html=files('forge').joinpath('web/index.html').read_bytes()
assert b'<html' in html.lower()
handler=handler_for(sys.argv[2],8765)
assert callable(handler.do_GET)
print('packaged HTML and in-memory handler available; no socket bound')
'''
        env = dict(os.environ)
        env.pop("PYTHONPATH", None)
        result = subprocess.run([sys.executable, "-c", script, str(archive), str(self.project)], cwd=self.base,
                                env=env, capture_output=True, text=True, timeout=20)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("no socket bound", result.stdout)


if __name__ == "__main__":
    unittest.main()
