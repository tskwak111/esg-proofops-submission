"""Offline workers must load without the POSIX-only paid transport lock."""

import subprocess
import sys


def test_offline_worker_import_does_not_require_fcntl():
    result = subprocess.run(
        [
            sys.executable,
            "-c",
            "import sys; sys.modules['fcntl'] = None; import proofops_worker.tag_runner",
        ],
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert result.returncode == 0, result.stderr
