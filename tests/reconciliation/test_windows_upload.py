"""Exercise real Windows kernel limits in disposable parser-like processes."""

import subprocess
import sys

import pytest


@pytest.mark.skipif(sys.platform != "win32", reason="Windows Job Object enforcement")
@pytest.mark.parametrize("operation", ["memory", "child_process"])
def test_windows_job_object_enforces_limits_in_child(operation):
    code = """
import subprocess, sys
from proofops.application.uploads_security import PdfLimits, _windows_limits
job = _windows_limits(PdfLimits(memory_bytes=128 * 1024 * 1024))
if sys.argv[1] == 'memory':
    try:
        value = bytearray(256 * 1024 * 1024)
    except MemoryError:
        print('memory_rejected')
    else:
        raise RuntimeError('memory limit was not enforced')
else:
    try:
        process = subprocess.Popen([sys.executable, '-c', 'pass'])
    except OSError:
        print('child_process_rejected')
    else:
        process.wait(timeout=5)
        raise RuntimeError('child process limit was not enforced')
"""
    result = subprocess.run(
        [sys.executable, "-c", code, operation],
        capture_output=True,
        text=True,
        timeout=15,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == f"{operation}_rejected"
