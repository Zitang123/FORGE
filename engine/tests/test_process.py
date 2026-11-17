import hashlib
import os
from pathlib import Path
import sys
import tempfile
import time
import unittest

from forge.process import execute


class ProcessTests(unittest.TestCase):
    def run_code(self, code, **options):
        return execute([sys.executable, "-c", code], Path.cwd(), **options)

    def test_success_and_hash(self):
        result = self.run_code("import sys; print('hello'); print('error',file=sys.stderr)")
        self.assertEqual(result["reason"], "completed")
        self.assertEqual(result["returncode"], 0)
        self.assertIn("hello", result["output"])
        self.assertIn("error", result["output"])
        self.assertEqual(result["bytes"], len(result["output"].encode()))
        self.assertEqual(result["output_sha256"], hashlib.sha256(result["output"].encode()).hexdigest())

    def test_nonzero(self):
        result = self.run_code("raise SystemExit(7)")
        self.assertEqual(result["returncode"], 7)
        self.assertEqual(result["reason"], "completed")

    def test_spawn_error(self):
        result = execute(["/no/such/executable/forge-test"], Path.cwd(), max_bytes=20)
        self.assertEqual(result["reason"], "spawn_error")
        self.assertIsNone(result["returncode"])
        self.assertLessEqual(result["bytes"], 20)

    def test_flood(self):
        result = self.run_code("import os\nwhile True: os.write(1,b'x'*65536)", max_bytes=12345, timeout=2)
        self.assertEqual(result["reason"], "output_limit")
        self.assertEqual(result["bytes"], 12345)
        self.assertEqual(result["output"], "x" * 12345)
        self.assertLess(result["seconds"], 2)

    def test_exact_limit_and_zero(self):
        result = self.run_code("import os; os.write(1,b'abc')", max_bytes=3)
        self.assertEqual(result["reason"], "completed")
        self.assertEqual(result["output"], "abc")
        result = self.run_code("print('x')", max_bytes=0)
        self.assertEqual(result["reason"], "output_limit")
        self.assertEqual(result["bytes"], 0)

    def test_hash_is_of_raw_bytes(self):
        result = self.run_code("import os; os.write(1,b'\\xff\\x00')")
        self.assertEqual(result["bytes"], 2)
        self.assertEqual(result["output"], "\ufffd\x00")
        self.assertEqual(result["output_sha256"], hashlib.sha256(b"\xff\x00").hexdigest())

    def test_timeout(self):
        result = self.run_code("import time; time.sleep(30)", timeout=0.15)
        self.assertEqual(result["reason"], "timeout")
        self.assertLess(result["seconds"], 1)

    def test_stdin(self):
        value = "Karina ⚽\n" * 20000
        result = self.run_code("import sys; print(len(sys.stdin.read()))", stdin=value)
        self.assertEqual(int(result["output"].strip()), len(value))
        result = self.run_code("import sys; print(repr(sys.stdin.read()))", stdin="")
        self.assertEqual(result["output"].strip(), "''")

    def test_stdin_broken_pipe(self):
        result = self.run_code("pass", stdin="x" * 1000000)
        self.assertEqual(result["returncode"], 0)

    def test_no_shell(self):
        result = execute([sys.executable, "-c", "import sys; print(sys.argv[1])", "$(touch forbidden); *"], Path.cwd())
        self.assertEqual(result["output"].strip(), "$(touch forbidden); *")

    def test_invalid_arguments(self):
        for args in ([], "echo", ["bad\0"]):
            with self.assertRaises(ValueError):
                execute(args, Path.cwd())
        for value in (0, -1, float("inf"), float("nan"), True, 10**1000):
            with self.assertRaises(ValueError):
                self.run_code("pass", timeout=value)
        for value in (-1, 1.5, True):
            with self.assertRaises(ValueError):
                self.run_code("pass", max_bytes=value)

    @unittest.skipUnless(os.name == "posix", "process groups require POSIX")
    def test_descendant_cleanup_on_parent_exit(self):
        with tempfile.TemporaryDirectory() as directory:
            marker = Path(directory) / "survived"
            child = "import time,pathlib; time.sleep(0.6); pathlib.Path(" + repr(str(marker)) + ").write_text('leaked')"
            parent = "import subprocess,sys; subprocess.Popen([sys.executable,'-c'," + repr(child) + "]); print('parent done')"
            result = self.run_code(parent, timeout=2)
            self.assertEqual(result["reason"], "completed")
            self.assertLess(result["seconds"], 1)
            time.sleep(0.7)
            self.assertFalse(marker.exists(), "descendant survived process-group cleanup")

    @unittest.skipUnless(os.name == "posix", "process groups require POSIX")
    def test_descendant_cleanup_on_timeout(self):
        with tempfile.TemporaryDirectory() as directory:
            marker = Path(directory) / "survived"
            child = "import time,pathlib; time.sleep(0.6); pathlib.Path(" + repr(str(marker)) + ").write_text('leaked')"
            parent = "import subprocess,sys,time; subprocess.Popen([sys.executable,'-c'," + repr(child) + "]); time.sleep(30)"
            result = self.run_code(parent, timeout=0.15)
            self.assertEqual(result["reason"], "timeout")
            time.sleep(0.7)
            self.assertFalse(marker.exists())


if __name__ == "__main__":
    unittest.main()
