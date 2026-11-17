"""Bounded POSIX command execution; this module is not a security sandbox.

Output is merged stdout/stderr. ``bytes`` and ``output_sha256`` describe the
retained raw bytes (before UTF-8 replacement decoding), never discarded bytes.
When the direct child exits, any remaining members of its process group are
killed; background workers must not outlive an execution. Deliberately detached
processes or processes outside this group are outside this module's guarantees.
"""
from __future__ import annotations

import hashlib
import math
import os
from pathlib import Path
import selectors
import signal
import subprocess
import time


def execute(argv: list[str], cwd: Path, timeout: float = 60,
            max_bytes: int = 1048576, stdin: str | None = None) -> dict:
    """Execute an argument vector with output/time limits and group cleanup.

    Invalid API arguments raise ValueError. Expected OS spawn failures return
    ``spawn_error`` with a bounded diagnostic. A timeout/output cap terminates
    the process group with SIGKILL rather than relying on cooperative shutdown.
    No shell interpolation, privilege changes, or sandbox bypass is performed.
    """
    if not isinstance(argv, list) or not argv or any(not isinstance(x, str) or "\0" in x for x in argv):
        raise ValueError("argv must be a nonempty list of strings without NUL")
    try:
        valid_timeout = not isinstance(timeout, bool) and isinstance(timeout, (float, int)) and math.isfinite(timeout) and timeout > 0
    except OverflowError:
        valid_timeout = False
    if not valid_timeout:
        raise ValueError("timeout must be a positive finite number")
    if isinstance(max_bytes, bool) or not isinstance(max_bytes, int) or max_bytes < 0:
        raise ValueError("max_bytes must be a nonnegative integer")
    if stdin is not None and not isinstance(stdin, str):
        raise ValueError("stdin must be text or None")
    started = time.monotonic()
    output = bytearray()
    reason = "completed"

    def result(returncode):
        return {"returncode": returncode, "reason": reason,
                "output": output.decode("utf-8", errors="replace"),
                "seconds": time.monotonic() - started,
                "output_sha256": hashlib.sha256(output).hexdigest(),
                "bytes": len(output)}

    try:
        process = subprocess.Popen(argv, cwd=cwd, stdin=subprocess.PIPE if stdin is not None else subprocess.DEVNULL,
                                   stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                   start_new_session=True, close_fds=True)
    except (OSError, ValueError) as error:
        output.extend(str(error).encode("utf-8", errors="replace")[:max_bytes])
        reason = "spawn_error"
        return result(None)

    def kill_group():
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        except PermissionError:
            # A host policy can forbid a group signal; direct-child kill is the
            # available fallback, not a guarantee of descendant cleanup.
            try:
                process.kill()
            except ProcessLookupError:
                pass

    selector = selectors.DefaultSelector()
    stdout_open = True
    input_offset = 0
    pending_input = b""
    parent_exited_at = None
    try:
        os.set_blocking(process.stdout.fileno(), False)
        selector.register(process.stdout, selectors.EVENT_READ, "stdout")
        if process.stdin is not None:
            if stdin:
                os.set_blocking(process.stdin.fileno(), False)
                selector.register(process.stdin, selectors.EVENT_WRITE, "stdin")
            else:
                process.stdin.close()

        while True:
            current = time.monotonic()
            if current - started >= timeout:
                reason = "timeout"
                kill_group()
                break
            returncode = process.poll()
            if returncode is not None and parent_exited_at is None:
                parent_exited_at = current
                kill_group()
                if process.stdin is not None and not process.stdin.closed:
                    try:
                        selector.unregister(process.stdin)
                    except KeyError:
                        pass
                    process.stdin.close()
            if returncode is not None and not stdout_open:
                break
            # Close inherited pipes after bounded cleanup even if an escaped
            # descendant keeps one open. Such a descendant is not sandboxed.
            if parent_exited_at is not None and current - parent_exited_at >= 0.25:
                break
            events = selector.select(min(0.025, max(0.0, timeout - (current - started))))
            for key, _ in events:
                if key.data == "stdout":
                    try:
                        chunk = os.read(key.fileobj.fileno(), min(65536, max_bytes - len(output) + 1))
                    except BlockingIOError:
                        continue
                    if not chunk:
                        selector.unregister(key.fileobj)
                        key.fileobj.close()
                        stdout_open = False
                        continue
                    available = max_bytes - len(output)
                    output.extend(chunk[:available])
                    if len(chunk) > available:
                        reason = "output_limit"
                        kill_group()
                        break
                else:
                    # Encode stdin progressively so a large input does not
                    # allocate a second full-sized input buffer.
                    if not pending_input and input_offset < len(stdin):
                        text_chunk = stdin[input_offset:input_offset + 16384]
                        pending_input = text_chunk.encode("utf-8", errors="replace")
                        input_offset += len(text_chunk)
                    try:
                        written = os.write(key.fileobj.fileno(), pending_input[:16384])
                        pending_input = pending_input[written:]
                    except BrokenPipeError:
                        pending_input = b""
                        input_offset = len(stdin)
                    except BlockingIOError:
                        continue
                    if not pending_input and input_offset >= len(stdin):
                        selector.unregister(key.fileobj)
                        key.fileobj.close()
            if reason == "output_limit":
                break
        if reason != "completed":
            kill_group()
        try:
            process.wait(timeout=0.25)
        except subprocess.TimeoutExpired:
            kill_group()
    finally:
        kill_group()
        selector.close()
        for stream in (process.stdin, process.stdout):
            if stream is not None and not stream.closed:
                stream.close()
        try:
            process.wait(timeout=0.25)
        except subprocess.TimeoutExpired:
            # Return remains bounded if host policy prevents termination.
            pass
    return result(process.poll())
