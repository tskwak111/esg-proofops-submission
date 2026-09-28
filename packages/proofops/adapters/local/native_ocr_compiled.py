"""Explicit, execution-only helper: a compiled-binary alternative to invoking
`swift <script>.swift <image>` as a fresh script-mode process per call.

Root cause (measured 2026-09-21, see tests/fixtures/pipeline/cold-native.md):
`_rendered_text` in the pinned `source_verification.py` spawns a brand-new
`swift native_ocr.swift <image>` process per rendered-text read. Script-mode
`swift` recompiles the source on every invocation; a precompiled binary of the
identical, unedited source skips that recompilation.

This module does NOT wire itself into any pinned reader. It is a single
explicit callable, `ensure_compiled_binary`, for a future caller (reported to
the coordinator as the integration seam) to opt into.

- Compiles the exact bytes read from the given `.swift` source with
  `swiftc -O` into a cache directory reused across calls in this process
  (one `TemporaryDirectory`, created lazily and cleaned up at process exit).
- Keys the compiled artifact by sha256 of those exact source bytes plus the
  resolved `swiftc` binary's real path, mtime/size, and its own
  `--version` output, so neither a changed source nor a changed/upgraded
  toolchain can ever reuse a stale binary.
- Every filesystem/subprocess step is inside the same try block; any
  failure (missing toolchain, unwritable cache, compile error, timeout)
  returns None instead of raising, so a caller can fall back to the
  original `swift <script> <image>` script-mode invocation.
- The compiled artifact is written to a per-call unique temp path inside
  the cache dir and atomically renamed into place, so concurrent callers in
  this process never race on a shared fixed filename.
"""

from __future__ import annotations

import os
import subprocess
import sys
import tempfile
from hashlib import sha256
from pathlib import Path
from threading import Lock

_DEFAULT_COMPILE_TIMEOUT_S = 120

_cache_dir_lock = Lock()
_cache_dir: tempfile.TemporaryDirectory | None = None


def _process_cache_dir() -> Path:
    """One lazily-created, process-private cache dir reused by every call.

    Not a fresh `mkdtemp` per invocation: repeated calls with the same
    source/toolchain reuse the compiled binary already written here.
    """
    global _cache_dir
    with _cache_dir_lock:
        if _cache_dir is None:
            _cache_dir = tempfile.TemporaryDirectory(prefix="proofops-native-ocr-compiled-")
        return Path(_cache_dir.name)


def ensure_compiled_binary(
    source_path: Path,
    *,
    developer_dir: str | None = None,
    cache_dir: Path | None = None,
    timeout_s: float = _DEFAULT_COMPILE_TIMEOUT_S,
) -> Path | None:
    """Return a compiled binary for `source_path`, compiling at most once per
    (exact source bytes, resolved toolchain identity) pair for this process.

    Returns None (never raises) on any failure — missing/unresolvable
    `swiftc`, unreadable source, unwritable cache, compile error, or
    timeout — so callers can safely fall back to the original
    `swift <source_path> <image>` script-mode invocation.
    """
    if sys.platform != "darwin":
        return None
    try:
        source_bytes = source_path.read_bytes()

        effective_env = dict(os.environ)
        if developer_dir:
            effective_env["DEVELOPER_DIR"] = developer_dir

        # `swiftc` on PATH can be the /usr/bin xcrun shim; resolve the actually
        # selected toolchain binary under the same effective env used to compile,
        # so identity and binary can never come from two different toolchains.
        resolved = (
            subprocess.run(
                ["xcrun", "--find", "swiftc"],
                capture_output=True,
                timeout=10,
                check=True,
                env=effective_env,
            )
            .stdout.decode("utf-8", "strict")
            .strip()
        )
        if not resolved:
            return None
        swiftc = resolved
        swiftc_stat = os.stat(swiftc)
        version_output = subprocess.run(
            [swiftc, "--version"],
            capture_output=True,
            timeout=10,
            check=True,
            env=effective_env,
        ).stdout

        digest = sha256()
        digest.update(source_bytes)
        digest.update(swiftc.encode("utf-8"))
        digest.update(str(swiftc_stat.st_mtime_ns).encode("utf-8"))
        digest.update(str(swiftc_stat.st_size).encode("utf-8"))
        digest.update(version_output)
        digest.update((developer_dir or "").encode("utf-8"))
        key = digest.hexdigest()

        root = cache_dir or _process_cache_dir()
        root.mkdir(parents=True, exist_ok=True)
        binary_path = root / f"{source_path.stem}-{key}"
        if binary_path.is_file() and os.access(binary_path, os.X_OK):
            return binary_path

        with tempfile.NamedTemporaryFile(
            dir=root, prefix=f"{source_path.stem}-{key}-", suffix=".tmp", delete=False
        ) as tmp_handle:
            tmp_path = Path(tmp_handle.name)
        try:
            with tempfile.NamedTemporaryFile(
                dir=root, prefix="source-", suffix=".swift", delete=False
            ) as source_copy_handle:
                source_copy_handle.write(source_bytes)
                source_copy_path = Path(source_copy_handle.name)
            try:
                subprocess.run(
                    ["xcrun", "swiftc", "-O", str(source_copy_path), "-o", str(tmp_path)],
                    check=True,
                    capture_output=True,
                    timeout=timeout_s,
                    env=effective_env,
                )
            finally:
                source_copy_path.unlink(missing_ok=True)
            os.chmod(tmp_path, 0o755)
            os.replace(tmp_path, binary_path)
        except (OSError, subprocess.SubprocessError):
            tmp_path.unlink(missing_ok=True)
            return None
        return binary_path if binary_path.is_file() else None
    except (OSError, subprocess.SubprocessError):
        return None
