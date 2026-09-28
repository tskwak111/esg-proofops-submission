"""Race-free Windows containment for the bounded parser subprocess.

The parser launcher spawns a JVM of its own, so containing only the launcher
would leave Java descendants alive. On POSIX the adapter uses a new session
plus `killpg`; the Windows equivalent is a Job Object, because a job is
inherited by every descendant and `TerminateJobObject` kills the whole tree in
one call.

Containment is established before the child runs a single instruction: the
process is created suspended, assigned to the job, and only then resumed. A
child that is never resumed cannot spawn anything, so no descendant can escape.

This helper is for the parser adapter only. It deliberately does not touch
`proofops.application.uploads_security`, whose job carries a one-process policy
that must stay exactly as it is.
"""

from __future__ import annotations

import subprocess
import sys
from collections.abc import Mapping, Sequence
from pathlib import Path
from types import TracebackType

# JOBOBJECT_EXTENDED_LIMIT_INFORMATION, per Microsoft's documented layout.
_EXTENDED_LIMIT_INFORMATION = 9
_LIMIT_PROCESS_TIME = 0x00000002
_LIMIT_PROCESS_MEMORY = 0x00000100
_LIMIT_JOB_MEMORY = 0x00000200
_LIMIT_DIE_ON_UNHANDLED_EXCEPTION = 0x00000400
_LIMIT_ACTIVE_PROCESS = 0x00000008
_LIMIT_KILL_ON_JOB_CLOSE = 0x00002000

_CREATE_SUSPENDED = 0x00000004
_CREATE_NO_WINDOW = 0x08000000
_CREATE_NEW_PROCESS_GROUP = 0x00000200
_THREAD_SUSPEND_RESUME = 0x0002
_SNAPTHREAD = 0x00000004

# A JVM launcher needs a handful of processes; anything beyond this is a fork
# bomb, not a parse. The kernel refuses the extra CreateProcess rather than
# letting the tree grow without bound.
MAX_ACTIVE_PROCESSES = 32
# Never wait unbounded on a process that may be suspended or already gone.
REAP_TIMEOUT_SECONDS = 15.0


class IsolationUnavailable(RuntimeError):
    """Windows refused to provide containment; the caller must fail closed."""


def kill_and_reap(process: subprocess.Popen, *, timeout: float = REAP_TIMEOUT_SECONDS) -> None:
    """Terminate one process by its own handle and wait for it, always bounded.

    A suspended child never exits on its own, so an unbounded `wait()` after a
    failed containment attempt would hang the parse forever. `Popen.kill` uses
    the handle from `CreateProcess`, which terminates a suspended process and
    cannot be aimed at a recycled pid.
    """
    try:
        process.kill()
    except (OSError, PermissionError):
        pass
    try:
        process.wait(timeout=timeout)
    except subprocess.TimeoutExpired:
        pass


class PosixSessionIsolation:
    """The original POSIX behaviour, unchanged: a new session plus `killpg`.

    Kept beside the Windows implementation so the adapter has one shape to call
    and neither platform's containment can be edited without seeing the other.
    """

    def __init__(self, *, memory_bytes: int, cpu_seconds: int) -> None:
        self._memory_bytes, self._cpu_seconds = memory_bytes, cpu_seconds

    def spawn(
        self,
        command: Sequence[str],
        *,
        cwd: Path,
        env: Mapping[str, str],
    ) -> subprocess.Popen:
        return subprocess.Popen(
            list(command),
            cwd=cwd,
            env=dict(env),
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
        )

    def peak_memory_bytes(self, process: subprocess.Popen) -> int:
        # Includes the SDK child and Java descendants, not only the launcher.
        rows = subprocess.run(
            ["/bin/ps", "-axo", "pgid=,rss="],
            capture_output=True,
            timeout=2,
            check=True,
        ).stdout.splitlines()
        return sum(
            int(parts[1]) * 1024
            for row in rows
            if len(parts := row.split()) == 2 and int(parts[0]) == process.pid
        )

    def memory_limit_exceeded(self, process: subprocess.Popen) -> bool:
        return self.peak_memory_bytes(process) > self._memory_bytes

    def terminate_tree(self, process: subprocess.Popen) -> None:
        if sys.platform == "win32":  # pragma: no cover - the factory never selects this here
            return
        import os
        import signal

        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass

    def close(self) -> None:
        return None


def process_isolation(*, memory_bytes: int, cpu_seconds: int):
    """Containment for one parser invocation, matched to the host platform."""
    if sys.platform == "win32":
        return WindowsJobIsolation(memory_bytes=memory_bytes, cpu_seconds=cpu_seconds)
    return PosixSessionIsolation(memory_bytes=memory_bytes, cpu_seconds=cpu_seconds)


def _kernel32():
    import ctypes
    from ctypes import wintypes

    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.CreateJobObjectW.argtypes = [ctypes.c_void_p, wintypes.LPCWSTR]
    kernel.CreateJobObjectW.restype = wintypes.HANDLE
    kernel.SetInformationJobObject.argtypes = [
        wintypes.HANDLE,
        ctypes.c_int,
        ctypes.c_void_p,
        wintypes.DWORD,
    ]
    kernel.SetInformationJobObject.restype = wintypes.BOOL
    kernel.QueryInformationJobObject.argtypes = [
        wintypes.HANDLE,
        ctypes.c_int,
        ctypes.c_void_p,
        wintypes.DWORD,
        ctypes.c_void_p,
    ]
    kernel.QueryInformationJobObject.restype = wintypes.BOOL
    kernel.AssignProcessToJobObject.argtypes = [wintypes.HANDLE, wintypes.HANDLE]
    kernel.AssignProcessToJobObject.restype = wintypes.BOOL
    kernel.TerminateJobObject.argtypes = [wintypes.HANDLE, wintypes.UINT]
    kernel.TerminateJobObject.restype = wintypes.BOOL
    kernel.CloseHandle.argtypes = [wintypes.HANDLE]
    kernel.CloseHandle.restype = wintypes.BOOL
    kernel.CreateToolhelp32Snapshot.argtypes = [wintypes.DWORD, wintypes.DWORD]
    kernel.CreateToolhelp32Snapshot.restype = wintypes.HANDLE
    kernel.OpenThread.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    kernel.OpenThread.restype = wintypes.HANDLE
    kernel.ResumeThread.argtypes = [wintypes.HANDLE]
    kernel.ResumeThread.restype = wintypes.DWORD
    kernel.Thread32First.argtypes = [wintypes.HANDLE, ctypes.c_void_p]
    kernel.Thread32First.restype = wintypes.BOOL
    kernel.Thread32Next.argtypes = [wintypes.HANDLE, ctypes.c_void_p]
    kernel.Thread32Next.restype = wintypes.BOOL
    return kernel


def _structures():
    import ctypes
    from ctypes import wintypes

    class Basic(ctypes.Structure):
        _fields_ = [
            ("process_time", ctypes.c_int64),
            ("job_time", ctypes.c_int64),
            ("flags", wintypes.DWORD),
            ("min_working_set", ctypes.c_size_t),
            ("max_working_set", ctypes.c_size_t),
            ("active_processes", wintypes.DWORD),
            ("affinity", ctypes.c_size_t),
            ("priority", wintypes.DWORD),
            ("scheduling", wintypes.DWORD),
        ]

    class Extended(ctypes.Structure):
        _fields_ = [
            ("basic", Basic),
            ("io_counters", ctypes.c_uint64 * 6),
            ("process_memory", ctypes.c_size_t),
            ("job_memory", ctypes.c_size_t),
            ("peak_process_memory", ctypes.c_size_t),
            ("peak_job_memory", ctypes.c_size_t),
        ]

    class ThreadEntry(ctypes.Structure):
        _fields_ = [
            ("size", wintypes.DWORD),
            ("usage", wintypes.DWORD),
            ("thread_id", wintypes.DWORD),
            ("owner_process_id", wintypes.DWORD),
            ("base_priority", ctypes.c_long),
            ("delta_priority", ctypes.c_long),
            ("flags", wintypes.DWORD),
        ]

    return Extended, ThreadEntry


def _require_windows() -> None:
    if sys.platform != "win32":
        raise IsolationUnavailable("windows_only")


class WindowsJobIsolation:
    """One job object per parser invocation, holding the launcher and its JVM."""

    def __init__(self, *, memory_bytes: int, cpu_seconds: int) -> None:
        _require_windows()
        import ctypes

        self._ctypes = ctypes
        self._kernel = _kernel32()
        self._extended, self._thread_entry = _structures()
        self._memory_bytes = memory_bytes
        self._job = self._kernel.CreateJobObjectW(None, None)
        if not self._job:
            raise IsolationUnavailable("create_job_object_failed")
        try:
            self._apply_limits(memory_bytes, cpu_seconds)
        except BaseException:
            self.close()
            raise

    def _apply_limits(self, memory_bytes: int, cpu_seconds: int) -> None:
        info = self._extended()
        info.basic.process_time = int(cpu_seconds) * 10_000_000
        info.basic.active_processes = MAX_ACTIVE_PROCESSES
        info.basic.flags = (
            _LIMIT_PROCESS_TIME
            | _LIMIT_PROCESS_MEMORY
            | _LIMIT_JOB_MEMORY
            | _LIMIT_ACTIVE_PROCESS
            | _LIMIT_DIE_ON_UNHANDLED_EXCEPTION
            | _LIMIT_KILL_ON_JOB_CLOSE
        )
        # The configured limit is the hard bound, not a value widened for
        # convenience: the kernel refuses commit beyond it. A refused allocation
        # can surface as a generic parser failure without exceeding the cap.
        info.process_memory = memory_bytes
        info.job_memory = memory_bytes
        if not self._kernel.SetInformationJobObject(
            self._job,
            _EXTENDED_LIMIT_INFORMATION,
            self._ctypes.byref(info),
            self._ctypes.sizeof(info),
        ):
            raise IsolationUnavailable("set_job_limits_failed")

    def spawn(
        self,
        command: Sequence[str],
        *,
        cwd: Path,
        env: Mapping[str, str],
    ) -> subprocess.Popen:
        """Create the child suspended, contain it, then let it run."""
        process = subprocess.Popen(
            list(command),
            cwd=str(cwd),
            env=dict(env),
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            stdin=subprocess.DEVNULL,
            creationflags=_CREATE_SUSPENDED | _CREATE_NO_WINDOW | _CREATE_NEW_PROCESS_GROUP,
        )
        try:
            handle = int(process._handle)  # type: ignore[attr-defined]
            if not self._kernel.AssignProcessToJobObject(self._job, handle):
                raise IsolationUnavailable("assign_to_job_failed")
            self._resume(process.pid)
        except BaseException:
            # The child is still suspended and has executed nothing at all, but
            # it may never have reached the job: terminating the job alone would
            # leave it alive and waiting on it would block forever. Terminate the
            # process by its own handle as well, and never wait unbounded.
            try:
                self.terminate_tree(process)
            finally:
                kill_and_reap(process)
            raise
        return process

    def _resume(self, pid: int) -> None:
        snapshot = self._kernel.CreateToolhelp32Snapshot(_SNAPTHREAD, 0)
        if snapshot == self._ctypes.c_void_p(-1).value or not snapshot:
            raise IsolationUnavailable("thread_snapshot_failed")
        try:
            entry = self._thread_entry()
            entry.size = self._ctypes.sizeof(entry)
            resumed = 0
            more = self._kernel.Thread32First(snapshot, self._ctypes.byref(entry))
            while more:
                if entry.owner_process_id == pid:
                    thread = self._kernel.OpenThread(_THREAD_SUSPEND_RESUME, False, entry.thread_id)
                    if thread:
                        try:
                            if self._kernel.ResumeThread(thread) != 0xFFFFFFFF:
                                resumed += 1
                        finally:
                            self._kernel.CloseHandle(thread)
                entry.size = self._ctypes.sizeof(entry)
                more = self._kernel.Thread32Next(snapshot, self._ctypes.byref(entry))
            if not resumed:
                raise IsolationUnavailable("resume_thread_failed")
        finally:
            self._kernel.CloseHandle(snapshot)

    def peak_memory_bytes(self, process: subprocess.Popen) -> int:
        """Peak committed memory across every process in the job, launcher and JVM."""
        del process
        info = self._extended()
        if not self._kernel.QueryInformationJobObject(
            self._job,
            _EXTENDED_LIMIT_INFORMATION,
            self._ctypes.byref(info),
            self._ctypes.sizeof(info),
            None,
        ):
            raise IsolationUnavailable("query_job_information_failed")
        return int(info.peak_job_memory)

    def memory_limit_exceeded(self, process: subprocess.Popen) -> bool:
        """True only when a sample actually observed more than the bound.

        The job's cap is the primary defence and is strict, so the usual
        Windows outcome is that the kernel refuses the allocation and the child
        dies: that surfaces as PARSER_FAILED, which is fail-closed. This
        watchdog stays as the second witness for the case where accounting lags
        far enough behind for a sample to see the breach, and it is the only
        mechanism on POSIX, where there is no kernel memory cap at all.
        """
        return self.peak_memory_bytes(process) > self._memory_bytes

    def terminate_tree(self, process: subprocess.Popen) -> None:
        """Kill the launcher and every descendant, whatever state they are in."""
        del process
        if self._job:
            if not self._kernel.TerminateJobObject(self._job, 1):
                raise IsolationUnavailable("terminate_job_failed")

    def close(self) -> None:
        if self._job:
            # Closing the last handle also enforces KILL_ON_JOB_CLOSE.
            self._kernel.CloseHandle(self._job)
            self._job = None

    def __enter__(self) -> WindowsJobIsolation:
        return self

    def __exit__(
        self,
        kind: type[BaseException] | None,
        value: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        self.close()
