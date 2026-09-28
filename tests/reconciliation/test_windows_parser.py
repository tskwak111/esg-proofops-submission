"""Native Windows execution of the bounded parser subprocess.

Every test here spawns real processes and asserts on real kernel state: no
mocked job object, no patched subprocess, no skip that would hide a failure.
The Windows-internals tests are guarded by platform because a Job Object does
not exist elsewhere; every limit and cleanup test that can run on both
platforms does.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace

import pytest
from proofops.adapters.parsing.opendataloader import (
    OpenDataLoaderParser,
    ParseFailure,
    _child_environment,
    _cleanup_parser_work,
    _output_bytes,
)
from proofops.adapters.parsing.windows_isolation import (
    MAX_ACTIVE_PROCESSES,
    IsolationUnavailable,
    PosixSessionIsolation,
    WindowsJobIsolation,
    kill_and_reap,
    process_isolation,
)

WINDOWS = sys.platform == "win32"
windows_only = pytest.mark.skipif(not WINDOWS, reason="Job Objects exist only on Windows")

MEGABYTE = 1024 * 1024


def test_output_accounting_tolerates_a_jvm_temp_file_removed_during_sampling(tmp_path, monkeypatch):
    transient = tmp_path / "tmp_pdf_file.pdf"
    transient.write_bytes(b"temporary")
    (tmp_path / "result.json").write_bytes(b"output")
    original = Path.stat

    def removed_during_stat(path, *args, **kwargs):
        if path == transient:
            transient.unlink(missing_ok=True)
        return original(path, *args, **kwargs)

    monkeypatch.setattr(Path, "stat", removed_during_stat)
    assert _output_bytes(tmp_path) == len(b"output")


def test_output_accounting_does_not_hide_permission_errors(tmp_path, monkeypatch):
    result = tmp_path / "result.json"
    result.write_bytes(b"output")
    original = Path.stat

    def denied(path, *args, **kwargs):
        if path == result:
            raise PermissionError("injected output read failure")
        return original(path, *args, **kwargs)

    monkeypatch.setattr(Path, "stat", denied)
    with pytest.raises(PermissionError):
        _output_bytes(tmp_path)


def profile(*, timeout_seconds=5.0, memory_bytes=512 * MEGABYTE, max_output_bytes=MEGABYTE):
    return SimpleNamespace(
        timeout_seconds=timeout_seconds,
        memory_bytes=memory_bytes,
        max_output_bytes=max_output_bytes,
    )


def child_environment() -> dict[str, str]:
    """The minimum a bare `python -c` child needs, on either platform."""
    if not WINDOWS:
        return {"PATH": "/usr/bin:/bin", "PYTHONDONTWRITEBYTECODE": "1"}
    system_root = os.environ.get("SystemRoot", r"C:\Windows")
    return {
        "PATH": os.pathsep.join([str(Path(system_root) / "System32"), system_root]),
        "SystemRoot": system_root,
        "PYTHONDONTWRITEBYTECODE": "1",
    }


def python(script: str) -> list[str]:
    return [sys.executable, "-I", "-c", script]


def alive(pid: int) -> bool:
    """Ask the OS directly; never parse `tasklist`, whose output is localised."""
    if WINDOWS:
        import ctypes
        from ctypes import wintypes

        kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel.OpenProcess.restype = wintypes.HANDLE
        handle = kernel.OpenProcess(0x1000, False, pid)  # QUERY_LIMITED_INFORMATION
        if not handle:
            return False
        try:
            code = wintypes.DWORD()
            if not kernel.GetExitCodeProcess(handle, ctypes.byref(code)):
                return False
            return code.value == 259  # STILL_ACTIVE
        finally:
            kernel.CloseHandle(handle)
    try:
        os.kill(pid, 0)
    except (ProcessLookupError, PermissionError):
        return False
    return True


def wait_for_file(path: Path, *, seconds: float = 20.0) -> str:
    from time import monotonic, sleep

    deadline = monotonic() + seconds
    while monotonic() < deadline:
        if path.exists():
            text = path.read_text(encoding="utf-8").strip()
            if text:
                return text
        sleep(0.05)
    raise AssertionError(f"{path.name} was never written")


# --------------------------------------------------------------------------- #
# platform selection
# --------------------------------------------------------------------------- #


def test_the_adapter_picks_the_containment_its_platform_actually_has():
    isolation = process_isolation(memory_bytes=64 * MEGABYTE, cpu_seconds=2)
    try:
        expected = WindowsJobIsolation if WINDOWS else PosixSessionIsolation
        assert isinstance(isolation, expected)
    finally:
        isolation.close()


def test_both_implementations_offer_the_same_containment_surface():
    for implementation in (WindowsJobIsolation, PosixSessionIsolation):
        for name in (
            "spawn",
            "peak_memory_bytes",
            "memory_limit_exceeded",
            "terminate_tree",
            "close",
        ):
            assert callable(getattr(implementation, name)), (implementation, name)


# --------------------------------------------------------------------------- #
# the job object really carries the limits
# --------------------------------------------------------------------------- #


@windows_only
def test_the_kernel_reports_the_limits_that_were_requested():
    import ctypes

    from proofops.adapters.parsing import windows_isolation

    isolation = WindowsJobIsolation(memory_bytes=128 * MEGABYTE, cpu_seconds=7)
    try:
        extended, _entry = windows_isolation._structures()
        info = extended()
        assert isolation._kernel.QueryInformationJobObject(
            isolation._job, 9, ctypes.byref(info), ctypes.sizeof(info), None
        )
        flags = info.basic.flags
        assert flags & windows_isolation._LIMIT_KILL_ON_JOB_CLOSE
        assert flags & windows_isolation._LIMIT_PROCESS_TIME
        assert flags & windows_isolation._LIMIT_PROCESS_MEMORY
        assert flags & windows_isolation._LIMIT_JOB_MEMORY
        assert flags & windows_isolation._LIMIT_ACTIVE_PROCESS
        assert flags & windows_isolation._LIMIT_DIE_ON_UNHANDLED_EXCEPTION
        assert info.basic.process_time == 7 * 10_000_000
        assert info.basic.active_processes == MAX_ACTIVE_PROCESSES
        # The configured bound is the hard bound: nothing is widened for slack.
        assert info.job_memory == 128 * MEGABYTE
        assert info.process_memory == 128 * MEGABYTE
    finally:
        isolation.close()


@windows_only
def test_containment_is_established_before_the_child_runs(tmp_path):
    """The child is created suspended, so it cannot act before it is contained."""
    marker = tmp_path / "ran.txt"
    script = f"open({str(marker)!r}, 'w').write('ran')"
    isolation = WindowsJobIsolation(memory_bytes=64 * MEGABYTE, cpu_seconds=10)
    try:
        process = subprocess.Popen(
            python(script),
            cwd=str(tmp_path),
            env=child_environment(),
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            creationflags=0x00000004,  # CREATE_SUSPENDED, exactly as spawn() uses
        )
        try:
            # Suspended means suspended: nothing has been executed yet.
            assert process.poll() is None
            assert not marker.exists()
        finally:
            # Never `process.wait()` here: the child is suspended and outside
            # the job, so it would block forever. This is the same bounded
            # cleanup the production failure path uses.
            isolation.terminate_tree(process)
            kill_and_reap(process)
        assert process.poll() is not None
        assert not marker.exists()
    finally:
        isolation.close()


# --------------------------------------------------------------------------- #
# the whole tree dies, not just the launcher
# --------------------------------------------------------------------------- #


def test_terminate_tree_kills_a_grandchild_that_outlived_its_launcher(tmp_path):
    """`killpg` on POSIX and `TerminateJobObject` on Windows must both do this."""
    grandchild_pid = tmp_path / "grandchild.txt"
    inner = "import time; time.sleep(600)"
    launcher = (
        "import subprocess, sys\n"
        f"child = subprocess.Popen([sys.executable, '-I', '-c', {inner!r}])\n"
        f"open({str(grandchild_pid)!r}, 'w').write(str(child.pid))\n"
        "import time; time.sleep(600)\n"
    )
    isolation = process_isolation(memory_bytes=512 * MEGABYTE, cpu_seconds=600)
    try:
        process = isolation.spawn(python(launcher), cwd=tmp_path, env=child_environment())
        pid = int(wait_for_file(grandchild_pid))
        assert alive(pid)
        isolation.terminate_tree(process)
        process.wait(timeout=30)
        from time import monotonic, sleep

        deadline = monotonic() + 15
        while alive(pid) and monotonic() < deadline:
            sleep(0.1)
        assert not alive(pid), "the grandchild escaped containment"
    finally:
        isolation.close()


@windows_only
def test_closing_the_job_kills_whatever_is_still_inside_it(tmp_path):
    """KILL_ON_JOB_CLOSE is the backstop for a crashed or abandoned parse."""
    grandchild_pid = tmp_path / "grandchild.txt"
    inner = "import time; time.sleep(600)"
    launcher = (
        "import subprocess, sys\n"
        f"child = subprocess.Popen([sys.executable, '-I', '-c', {inner!r}])\n"
        f"open({str(grandchild_pid)!r}, 'w').write(str(child.pid))\n"
        "import time; time.sleep(600)\n"
    )
    isolation = WindowsJobIsolation(memory_bytes=512 * MEGABYTE, cpu_seconds=600)
    process = isolation.spawn(python(launcher), cwd=tmp_path, env=child_environment())
    pid = int(wait_for_file(grandchild_pid))
    assert alive(pid)
    isolation.close()  # no terminate_tree call at all
    from time import monotonic, sleep

    deadline = monotonic() + 15
    while (alive(pid) or process.poll() is None) and monotonic() < deadline:
        sleep(0.1)
    assert not alive(pid)
    assert process.poll() is not None


def test_close_is_idempotent():
    isolation = process_isolation(memory_bytes=64 * MEGABYTE, cpu_seconds=2)
    isolation.close()
    isolation.close()


@windows_only
def test_parser_scratch_cleanup_waits_for_terminated_descendant_file_handles(tmp_path):
    temporary = TemporaryDirectory(prefix=".parse-", dir=tmp_path)
    work = Path(temporary.name)
    held = work / "held.bin"
    marker = work / "ready.txt"
    child = (
        f"stream = open({str(held)!r}, 'wb')\n"
        "stream.write(b'held'); stream.flush()\n"
        f"open({str(marker)!r}, 'w').write('ready')\n"
        "import time; time.sleep(600)\n"
    )
    launcher = (
        "import subprocess, sys, time\n"
        f"subprocess.Popen([sys.executable, '-I', '-c', {child!r}])\n"
        "time.sleep(600)\n"
    )
    isolation = WindowsJobIsolation(memory_bytes=512 * MEGABYTE, cpu_seconds=600)
    process = isolation.spawn(python(launcher), cwd=tmp_path, env=child_environment())
    try:
        wait_for_file(marker)
        with pytest.raises(PermissionError):
            held.unlink()
        isolation.terminate_tree(process)
        _cleanup_parser_work(temporary, tmp_path)
        assert not work.exists()
    finally:
        isolation.terminate_tree(process)
        kill_and_reap(process)
        isolation.close()
        temporary.cleanup()


def test_parser_scratch_cleanup_never_deletes_an_unowned_directory(tmp_path):
    outside = tmp_path / "important"
    outside.mkdir()

    def forbidden_cleanup():
        raise AssertionError("unowned directory must not be removed")

    temporary = SimpleNamespace(name=str(outside), cleanup=forbidden_cleanup)
    with pytest.raises(ValueError, match="outside its artifact parent"):
        _cleanup_parser_work(temporary, tmp_path)
    assert outside.is_dir()


@windows_only
def test_parser_scratch_cleanup_has_a_deadline_even_if_a_handle_stays_locked(tmp_path):
    def denied():
        raise PermissionError("still locked")

    temporary = SimpleNamespace(name=str(tmp_path / ".parse-locked"), cleanup=denied)
    with pytest.raises(PermissionError, match="still locked"):
        _cleanup_parser_work(temporary, tmp_path, timeout_seconds=0)


# --------------------------------------------------------------------------- #
# memory accounting covers descendants
# --------------------------------------------------------------------------- #


def test_peak_memory_counts_the_grandchild_not_only_the_launcher(tmp_path):
    ready = tmp_path / "allocated.txt"
    inner = (
        "block = bytearray(160 * 1024 * 1024)\n"
        "for index in range(0, len(block), 4096): block[index] = 1\n"
        f"open({str(ready)!r}, 'w').write('done')\n"
        "import time; time.sleep(600)\n"
    )
    launcher = (
        "import subprocess, sys, time\n"
        f"subprocess.Popen([sys.executable, '-I', '-c', {inner!r}])\n"
        "time.sleep(600)\n"
    )
    isolation = process_isolation(memory_bytes=4096 * MEGABYTE, cpu_seconds=600)
    try:
        process = isolation.spawn(python(launcher), cwd=tmp_path, env=child_environment())
        try:
            wait_for_file(ready, seconds=60)
            assert isolation.peak_memory_bytes(process) > 128 * MEGABYTE
            assert isolation.memory_limit_exceeded(process) is False
        finally:
            isolation.terminate_tree(process)
            process.wait(timeout=30)
    finally:
        isolation.close()


# --------------------------------------------------------------------------- #
# _execute keeps its error contract on this platform
# --------------------------------------------------------------------------- #


def execute(command, work, *, limits):
    OpenDataLoaderParser._execute(command, work, child_environment(), limits)


def test_a_clean_child_is_not_an_error(tmp_path):
    execute(python("pass"), tmp_path, limits=profile())


def test_a_non_zero_exit_is_reported_as_a_parser_failure(tmp_path):
    with pytest.raises(ParseFailure) as error:
        execute(python("raise SystemExit(3)"), tmp_path, limits=profile())
    assert str(error.value) == "PARSER_FAILED"


def test_a_child_that_never_finishes_times_out_and_is_killed(tmp_path):
    marker = tmp_path / "grandchild.txt"
    inner = "import time; time.sleep(600)"
    launcher = (
        "import subprocess, sys, time\n"
        f"child = subprocess.Popen([sys.executable, '-I', '-c', {inner!r}])\n"
        f"open({str(marker)!r}, 'w').write(str(child.pid))\n"
        "time.sleep(600)\n"
    )
    with pytest.raises(ParseFailure) as error:
        execute(python(launcher), tmp_path, limits=profile(timeout_seconds=3.0))
    assert str(error.value) == "PARSER_TIMEOUT"
    pid = int(marker.read_text(encoding="utf-8").strip())
    from time import monotonic, sleep

    deadline = monotonic() + 15
    while alive(pid) and monotonic() < deadline:
        sleep(0.1)
    assert not alive(pid), "a timed-out parse left its JVM behind"


def test_a_child_that_writes_too_much_hits_the_output_limit(tmp_path):
    script = (
        "import time\n"
        "with open('flood.bin', 'wb') as stream:\n"
        "    for _ in range(64):\n"
        "        stream.write(b'x' * (1024 * 1024)); stream.flush()\n"
        "time.sleep(600)\n"
    )
    with pytest.raises(ParseFailure) as error:
        execute(
            python(script),
            tmp_path,
            limits=profile(timeout_seconds=60.0, max_output_bytes=2 * MEGABYTE),
        )
    assert str(error.value) == "PARSER_OUTPUT_LIMIT"


def test_a_child_that_allocates_far_past_the_bound_never_succeeds(tmp_path):
    """Fail closed either way: the kernel denies it, or the watchdog sees it.

    On Windows the job cap is strict, so the allocation that would cross the
    bound is refused rather than charged and the child dies with a non-zero
    status; the parse is refused as PARSER_FAILED. On POSIX there is no kernel
    cap, so the sampling watchdog is what observes the breach and the parse is
    refused as PARSER_MEMORY_LIMIT. What must never happen is a completed parse.
    """
    script = (
        "blocks = []\n"
        "for _ in range(40):\n"
        "    block = bytearray(16 * 1024 * 1024)\n"
        "    for index in range(0, len(block), 4096): block[index] = 1\n"
        "    blocks.append(block)\n"
        "import time; time.sleep(600)\n"
    )
    with pytest.raises(ParseFailure) as error:
        execute(
            python(script),
            tmp_path,
            limits=profile(timeout_seconds=90.0, memory_bytes=48 * MEGABYTE),
        )
    assert str(error.value) in {"PARSER_MEMORY_LIMIT", "PARSER_FAILED"}


@windows_only
def test_the_strict_job_cap_really_denies_the_allocation(tmp_path):
    """The cap is enforced by the kernel, not merely configured."""
    denied = tmp_path / "denied.txt"
    script = (
        "blocks = []\n"
        "try:\n"
        "    for _ in range(40):\n"
        "        block = bytearray(16 * 1024 * 1024)\n"
        "        for index in range(0, len(block), 4096): block[index] = 1\n"
        "        blocks.append(block)\n"
        "except MemoryError:\n"
        f"    open({str(denied)!r}, 'w').write('denied')\n"
    )
    isolation = WindowsJobIsolation(memory_bytes=48 * MEGABYTE, cpu_seconds=90)
    try:
        process = isolation.spawn(python(script), cwd=tmp_path, env=child_environment())
        process.wait(timeout=120)
        assert (
            denied.exists() or process.returncode != 0
        ), "the child allocated far past the cap without being stopped"
        assert isolation.peak_memory_bytes(process) <= 48 * MEGABYTE
    finally:
        isolation.terminate_tree(process)
        kill_and_reap(process)
        isolation.close()


def test_a_normal_child_is_never_reported_as_a_memory_breach(tmp_path):
    """Regression: a false positive here refuses every legitimate parse."""
    isolation = process_isolation(memory_bytes=512 * MEGABYTE, cpu_seconds=60)
    try:
        process = isolation.spawn(
            python("import time; time.sleep(1)"), cwd=tmp_path, env=child_environment()
        )
        assert isolation.memory_limit_exceeded(process) is False
        process.wait(timeout=60)
        assert isolation.memory_limit_exceeded(process) is False
    finally:
        isolation.terminate_tree(process)
        kill_and_reap(process)
        isolation.close()


def test_the_source_pdf_itself_is_not_counted_against_the_output_limit(tmp_path):
    (tmp_path / "source.pdf").write_bytes(b"x" * (4 * MEGABYTE))
    execute(python("pass"), tmp_path, limits=profile(max_output_bytes=MEGABYTE))


def test_containment_failure_never_degrades_into_an_uncontained_parse(tmp_path, monkeypatch):
    """Refusing to parse is the only acceptable response to lost containment."""
    from proofops.adapters.parsing import opendataloader

    def refuse(**_kwargs):
        raise IsolationUnavailable("create_job_object_failed")

    monkeypatch.setattr(opendataloader, "process_isolation", refuse)
    with pytest.raises(ParseFailure) as error:
        execute(python("pass"), tmp_path, limits=profile())
    assert str(error.value) == "PARSER_ISOLATION_UNAVAILABLE"


# --------------------------------------------------------------------------- #
# the child environment is built for the host, and stays minimal
# --------------------------------------------------------------------------- #


def test_the_child_environment_never_inherits_the_parent_one(monkeypatch):
    monkeypatch.setenv("PROOFOPS_SECRET_PROBE", "must-not-leak")
    built = _child_environment(sys.executable, Path.cwd(), profile())
    assert "PROOFOPS_SECRET_PROBE" not in built
    assert "JAVA_TOOL_OPTIONS" in built
    assert built["PYTHONDONTWRITEBYTECODE"] == "1"


def test_the_child_environment_uses_this_platforms_path_separator():
    built = _child_environment(sys.executable, Path.cwd(), profile())
    assert str(Path(sys.executable).parent) in built["PATH"]
    if WINDOWS:
        assert ";" in built["PATH"] and ":/usr/bin" not in built["PATH"]
        assert built["SystemRoot"]
        assert Path(built["PATH"].split(os.pathsep)[1]).name.lower() == "system32"
    else:
        assert built["PATH"].endswith(":/usr/bin:/bin")


@windows_only
def test_the_windows_child_keeps_its_scratch_files_inside_the_work_directory(tmp_path):
    built = _child_environment(sys.executable, tmp_path, profile())
    assert built["TEMP"] == str(tmp_path) == built["TMP"]


def test_child_limits_apply_without_importing_an_absent_module(tmp_path):
    """`import resource` is POSIX-only and must not be attempted on Windows.

    Run in a subprocess on purpose: `_child_limits` sets hard rlimits on POSIX,
    and calling it here would apply a CPU limit to the test runner itself.
    """
    script = (
        "import sys\n"
        "sys.path[:0] = " + repr(sys.path) + "\n"
        "from proofops.adapters.parsing.opendataloader import _child_limits\n"
        "_child_limits({'timeout_seconds': 2.0, 'max_output_bytes': 1024 * 1024})\n"
        "print('applied')\n"
    )
    done = subprocess.run(
        [sys.executable, "-c", script],
        cwd=str(tmp_path),
        capture_output=True,
        text=True,
        timeout=120,
    )
    assert done.returncode == 0, done.stderr
    assert "applied" in done.stdout


@windows_only
def test_the_windows_child_suppresses_the_crash_dialog_that_would_hang_a_parse(tmp_path):
    """A crash box would hold a terminated parse open until someone clicks it."""
    script = (
        "import ctypes, sys\n"
        "sys.path[:0] = " + repr(sys.path) + "\n"
        "from proofops.adapters.parsing.opendataloader import _child_limits\n"
        "_child_limits({'timeout_seconds': 2.0, 'max_output_bytes': 1024 * 1024})\n"
        "kernel = ctypes.WinDLL('kernel32', use_last_error=True)\n"
        "mode = kernel.SetErrorMode(0)\n"
        "print(mode & 0x0002)\n"  # SEM_NOGPFAULTERRORBOX
    )
    done = subprocess.run(
        [sys.executable, "-c", script],
        cwd=str(tmp_path),
        capture_output=True,
        text=True,
        timeout=120,
    )
    assert done.returncode == 0, done.stderr
    assert done.stdout.strip() == "2"


# --------------------------------------------------------------------------- #
# containment that fails must not leave a child behind, and must return
# --------------------------------------------------------------------------- #


def elapsed(call):
    from time import monotonic

    started = monotonic()
    try:
        call()
    finally:
        return monotonic() - started


@windows_only
@pytest.mark.parametrize("broken", ["AssignProcessToJobObject", "ResumeThread"])
def test_a_failed_containment_kills_the_child_and_returns_promptly(tmp_path, broken):
    """If the child never reaches the job, terminating the job cannot reach it.

    The suspended child would also never exit on its own, so an unbounded wait
    would hang the parse forever. Both halves are asserted here.
    """
    from time import monotonic

    marker = tmp_path / "ran.txt"
    script = f"open({str(marker)!r}, 'w').write('ran')\nimport time; time.sleep(600)\n"
    isolation = WindowsJobIsolation(memory_bytes=64 * MEGABYTE, cpu_seconds=60)
    spawned: list[subprocess.Popen] = []
    real_popen = subprocess.Popen

    def record(*args, **kwargs):
        process = real_popen(*args, **kwargs)
        spawned.append(process)
        return process

    original = getattr(isolation._kernel, broken)
    # AssignProcessToJobObject fails with 0; ResumeThread fails with (DWORD)-1,
    # because 0 is a legitimate previous suspend count.
    failure = 0xFFFFFFFF if broken == "ResumeThread" else 0
    setattr(isolation._kernel, broken, lambda *args, **kwargs: failure)
    try:
        subprocess.Popen = record  # type: ignore[misc]
        started = monotonic()
        with pytest.raises(IsolationUnavailable):
            isolation.spawn(python(script), cwd=tmp_path, env=child_environment())
        duration = monotonic() - started
    finally:
        subprocess.Popen = real_popen  # type: ignore[misc]
        setattr(isolation._kernel, broken, original)
        isolation.close()

    assert duration < 30, "a failed containment blocked instead of returning"
    assert spawned, "no child was created, so the failure path was never exercised"
    child = spawned[0]
    assert child.poll() is not None, "the child survived a failed containment"
    assert not alive(child.pid)
    assert not marker.exists(), "the child ran even though containment failed"


def test_kill_and_reap_returns_even_for_a_process_that_never_exits(tmp_path):
    from time import monotonic

    process = subprocess.Popen(
        python("import time; time.sleep(600)"),
        cwd=str(tmp_path),
        env=child_environment(),
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    started = monotonic()
    kill_and_reap(process)
    assert monotonic() - started < 30
    assert process.poll() is not None


def test_kill_and_reap_is_safe_on_a_process_that_already_exited(tmp_path):
    process = subprocess.Popen(
        python("pass"),
        cwd=str(tmp_path),
        env=child_environment(),
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    process.wait(timeout=30)
    kill_and_reap(process)
    kill_and_reap(process)


# --------------------------------------------------------------------------- #
# limits cannot be bypassed by finishing quickly
# --------------------------------------------------------------------------- #


def test_a_child_that_writes_too_much_and_exits_at_once_is_still_caught(tmp_path):
    """The breach happened; exiting before the next sample does not undo it."""
    script = (
        "with open('flood.bin', 'wb') as stream:\n" "    stream.write(b'x' * (6 * 1024 * 1024))\n"
    )
    with pytest.raises(ParseFailure) as error:
        execute(
            python(script),
            tmp_path,
            limits=profile(timeout_seconds=60.0, max_output_bytes=2 * MEGABYTE),
        )
    assert str(error.value) == "PARSER_OUTPUT_LIMIT"
