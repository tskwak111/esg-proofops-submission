"""SEC-002 quarantine verification. No viewer, shell, model, or remote resource execution.

LocalUploadVault is a synthetic filesystem adapter, never an S3 implementation.
The upload API must supply server-owned object/version metadata and session tenant.
"""

from __future__ import annotations

import json
import math
import os
import re
import subprocess
import sys
from dataclasses import asdict, dataclass, field
from hashlib import sha256
from pathlib import Path
from tempfile import NamedTemporaryFile, TemporaryDirectory
from time import monotonic


class UploadRejected(ValueError):
    """Code-only failure; never include PDF contents, original filenames, or parser stderr."""


@dataclass(frozen=True)
class PdfLimits:
    # Technical development limits, not corpus performance claims.
    max_bytes: int = 104_857_600
    max_pages: int = 300
    max_objects: int = 100_000
    max_decoded_bytes: int = 75_000_000
    timeout_seconds: float = 30
    memory_bytes: int = 512 * 1024 * 1024
    cpu_seconds: int = 20

    def __post_init__(self) -> None:
        for key, value in asdict(self).items():
            if (
                isinstance(value, bool)
                or not isinstance(value, float | int)
                or not math.isfinite(value)
                or value <= 0
                or (key != "timeout_seconds" and not isinstance(value, int))
            ):
                raise ValueError("Positive finite resource limits required")


@dataclass(frozen=True)
class QuarantinedPdf:
    tenant_id: str
    document_version_id: str
    object_version_id: str
    content: bytes = field(repr=False)
    expected_size: int
    expected_sha256: str


@dataclass(frozen=True)
class VerifiedPdf:
    source: QuarantinedPdf
    page_count: int


def verify_quarantined_pdf(
    source: QuarantinedPdf,
    limits: PdfLimits,
    *,
    tenant_id: str,
) -> VerifiedPdf:
    """Verify an immutable quarantine snapshot; resource failures never become evidence absence.

    Storage callers must bound object reads by max_bytes before constructing the snapshot.
    On Windows a Job Object limits committed memory, CPU time and child processes.
    On Linux the child has hard address-space/CPU/process limits. macOS uses an RSS
    watchdog (sampled, not a kernel memory sandbox); deployment isolation is separate.
    """
    if tenant_id != source.tenant_id:
        raise UploadRejected("NOT_FOUND")
    from proofops.domain.values import _require_uuid

    try:
        _require_uuid("tenant_id", tenant_id)
        _require_uuid("document_version_id", source.document_version_id)
    except ValueError:
        raise UploadRejected("PDF_INVALID") from None
    if (
        not isinstance(source.object_version_id, str)
        or not 1 <= len(source.object_version_id) <= 1024
        or any(ord(character) < 32 for character in source.object_version_id)
    ):
        raise UploadRejected("PDF_INVALID")
    if not isinstance(source.content, bytes):
        raise UploadRejected("PDF_INVALID")
    if len(source.content) > limits.max_bytes:
        raise UploadRejected("UPLOAD_LIMIT_EXCEEDED")
    if (
        type(source.expected_size) is not int
        or source.expected_size != len(source.content)
        or sha256(source.content).hexdigest() != source.expected_sha256
    ):
        raise UploadRejected("UPLOAD_INTEGRITY_MISMATCH")
    if not re.match(rb"%PDF-(?:1\.[0-7]|2\.0)[\r\n]", source.content):
        raise UploadRejected("PDF_INVALID")
    with TemporaryDirectory(prefix="proofops-quarantine-") as scratch:
        input_path = Path(scratch) / "source.pdf"
        input_path.write_bytes(source.content)
        input_path.chmod(0o400)
        command = [
            sys.executable,
            "-I",
            str(Path(__file__).resolve()),
            str(input_path),
            json.dumps(asdict(limits)),
        ]
        deadline = monotonic() + limits.timeout_seconds
        with subprocess.Popen(
            command,
            shell=False,
            cwd=scratch,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            env={"PYTHONDONTWRITEBYTECODE": "1"},
            start_new_session=True,
        ) as process:
            try:
                while True:
                    remaining = deadline - monotonic()
                    if remaining <= 0:
                        raise UploadRejected("UPLOAD_LIMIT_EXCEEDED")
                    try:
                        output, _ = process.communicate(timeout=min(0.02, remaining))
                        break
                    except subprocess.TimeoutExpired:
                        if sys.platform == "darwin":
                            # ponytail: sampled macOS RSS; Linux hard limits for production.
                            rss = subprocess.run(
                                ["/bin/ps", "-o", "rss=", "-p", str(process.pid)],
                                capture_output=True,
                                timeout=1,
                                check=False,
                            ).stdout.strip()
                            if rss and int(rss) * 1024 > limits.memory_bytes:
                                raise UploadRejected("UPLOAD_LIMIT_EXCEEDED")
                if process.returncode:
                    raise UploadRejected(
                        "UPLOAD_LIMIT_EXCEEDED" if process.returncode < 0 else "PDF_INVALID"
                    )
                result = json.loads(output)
                if result["code"] != "verified":
                    raise UploadRejected(result["code"])
            finally:
                if process.poll() is None:
                    process.kill()
                process.wait()
    return VerifiedPdf(source, result["pages"])


class LocalUploadVault:
    """Synthetic local adapter: private root, atomic create-only promotion, no AWS calls.

    Use only with a service-owned root; uploaders must never have filesystem access.
    Upload state/CAS and the production versioned S3 adapter remain separate.
    """

    def __init__(self, root: Path):
        self.root = root

    def original_path(self, verified: VerifiedPdf) -> Path:
        source = verified.source
        return self.root / "original" / source.tenant_id / f"{source.document_version_id}.pdf"

    def verify_and_promote(
        self,
        source: QuarantinedPdf,
        limits: PdfLimits,
        *,
        tenant_id: str,
    ) -> VerifiedPdf:
        verified = verify_quarantined_pdf(source, limits, tenant_id=tenant_id)
        target = self.original_path(verified)
        target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        with NamedTemporaryFile(dir=target.parent) as staged:
            staged.write(source.content)
            staged.flush()
            os.fsync(staged.fileno())
            os.chmod(staged.name, 0o400)
            try:
                os.link(staged.name, target)  # Atomic create-only; never replace a prior revision.
            except FileExistsError:
                if target.is_symlink() or target.read_bytes() != source.content:
                    raise UploadRejected("VERSION_CONFLICT") from None
        return verified


def _windows_limits(limits: PdfLimits) -> int:
    """Attach this parser child to a fail-closed Windows Job Object.

    The returned OS handle is intentionally held until process exit. This helper
    must never be called in the API process. Layouts/flags follow Microsoft's
    JOBOBJECT_EXTENDED_LIMIT_INFORMATION and JOBOBJECT_BASIC_LIMIT_INFORMATION.
    """
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

    kernel = getattr(ctypes, "WinDLL")("kernel32", use_last_error=True)
    kernel.CreateJobObjectW.argtypes = [ctypes.c_void_p, wintypes.LPCWSTR]
    kernel.CreateJobObjectW.restype = wintypes.HANDLE
    kernel.GetCurrentProcess.restype = wintypes.HANDLE
    kernel.SetInformationJobObject.argtypes = [
        wintypes.HANDLE,
        ctypes.c_int,
        ctypes.c_void_p,
        wintypes.DWORD,
    ]
    kernel.AssignProcessToJobObject.argtypes = [wintypes.HANDLE, wintypes.HANDLE]
    kernel.QueryInformationJobObject.argtypes = [
        wintypes.HANDLE,
        ctypes.c_int,
        ctypes.c_void_p,
        wintypes.DWORD,
        ctypes.c_void_p,
    ]
    kernel.CloseHandle.argtypes = [wintypes.HANDLE]
    job = kernel.CreateJobObjectW(None, None)
    if not job:
        raise UploadRejected("PDF_INVALID")
    if not kernel.AssignProcessToJobObject(job, kernel.GetCurrentProcess()):
        kernel.CloseHandle(job)
        raise UploadRejected("PDF_INVALID")
    baseline = Extended()
    if not kernel.QueryInformationJobObject(
        job,
        9,
        ctypes.byref(baseline),
        ctypes.sizeof(baseline),
        None,
    ):
        kernel.CloseHandle(job)
        raise UploadRejected("PDF_INVALID")
    if baseline.peak_process_memory > limits.memory_bytes:
        kernel.CloseHandle(job)
        raise UploadRejected("UPLOAD_LIMIT_EXCEEDED")
    info = Extended()
    info.basic.process_time = limits.cpu_seconds * 10_000_000
    info.basic.active_processes = 1
    info.basic.flags = 0x00000002 | 0x00000008 | 0x00000100
    info.process_memory = limits.memory_bytes
    if not kernel.SetInformationJobObject(job, 9, ctypes.byref(info), ctypes.sizeof(info)):
        kernel.CloseHandle(job)
        raise UploadRejected("PDF_INVALID")
    return job


def _inspect(path: str, limits: PdfLimits) -> int:
    # Only the resource-limited child imports/parses untrusted PDF structures.
    if sys.platform != "win32":
        import resource

        resource.setrlimit(resource.RLIMIT_CPU, (limits.cpu_seconds, limits.cpu_seconds))
        resource.setrlimit(resource.RLIMIT_CORE, (0, 0))
    from pypdf import PdfReader, apply_configuration
    from pypdf.generic import (
        ArrayObject,
        BooleanObject,
        DictionaryObject,
        IndirectObject,
        PdfObject,
        StreamObject,
    )

    if sys.platform == "win32":
        _windows_limits(limits)
    elif sys.platform == "linux":
        # Load trusted parser code before constraining its address space. An undersized
        # budget must be a resource rejection, not a shared-library import/PDF error.
        current_vm = int(Path("/proc/self/statm").read_text().split()[0]) * resource.getpagesize()
        if current_vm > limits.memory_bytes:
            raise UploadRejected("UPLOAD_LIMIT_EXCEEDED")
        resource.setrlimit(resource.RLIMIT_AS, (limits.memory_bytes, limits.memory_bytes))
        resource.setrlimit(resource.RLIMIT_NPROC, (32, 32))

    with apply_configuration(
        maximum_declared_stream_length=limits.max_bytes,
        array_based_stream_maximum_output_length=limits.max_decoded_bytes,
        zlib_maximum_output_length=limits.max_decoded_bytes,
        lzw_maximum_output_length=limits.max_decoded_bytes,
        run_length_maximum_output_length=limits.max_decoded_bytes,
        jbig2dec_binary=None,
        page_tree_maximum_entries=limits.max_objects,
    ):
        reader = PdfReader(path, strict=True)
        if reader.is_encrypted:
            raise UploadRejected("PDF_PASSWORD_REQUIRED")
        pages = len(reader.pages)
        if pages == 0:
            raise UploadRejected("PDF_INVALID")
        if pages > limits.max_pages:
            raise UploadRejected("UPLOAD_LIMIT_EXCEEDED")
        # Inspect every indexed object, including compressed and otherwise unreachable actions.
        references = [
            (number, generation)
            for generation, entries in reader.xref.items()
            for number in entries
            if number != 0 and generation != 65535
        ]
        references.extend((number, 0) for number in reader.xref_objStm)
        if len(references) > limits.max_objects:
            raise UploadRejected("UPLOAD_LIMIT_EXCEEDED")
        pending: list[PdfObject | None] = [
            IndirectObject(number, generation, reader) for number, generation in references
        ]
        seen: set[int] = set()
        decoded = 0
        # Action dictionaries reachable from /AA, /OpenAction, or an annotation's
        # direct /A are validated by explicit subtype allowlist below; they must
        # never be treated as merely "present -> reject" (that rejected inert
        # /D -> /Named navigation) nor skipped as "present -> ignore" (that would
        # defeat fail-closed intent).
        action_table_keys = {"/AA"}
        direct_action_keys = {"/OpenAction", "/A"}
        # /A also names structure-element attribute objects/arrays in tagged
        # PDFs; only treat it as an action when its value is action-shaped
        # (a dict carrying /S), never a bare attribute dict/array.
        # These constructs are inherently dangerous wherever they occur, including
        # inside an action dictionary's own keys (e.g. a /Launch action's /F).
        forbidden = {
            "/JavaScript",
            "/JS",
            "/Launch",
            "/SubmitForm",
            "/ImportData",
            "/GoToR",
            "/GoToE",
            "/Rendition",
            "/RichMedia",
            "/EmbeddedFiles",
            "/EmbeddedFile",
            "/XFA",
        }
        # Inert by allowlist: pure in-viewer navigation with no code execution,
        # filesystem, or network access. /Named and /GoTo are in-viewer
        # navigation and remain inert even for automatic triggers. /URI (data
        # only, never fetched here) and /Hide are only allowed for explicit
        # user-gesture triggers -- annotation/outline /A and Widget mouse
        # down/up /AA D//U -- never for automatic /OpenAction or automatic /AA
        # events, which stay rejected. /Hide toggles visibility of this
        # document's own annotations/form fields (ISO 32000-1 Table 210 /
        # PDF 1.7 Table 8.60): its /T names an annotation dictionary, a text
        # field name string, or an array of those, and its optional /H is a
        # boolean; it never resolves a file, URL, or embedded target. The
        # Widget mouse-down/up exception is applied at its /AA call site.
        inert_action_subtypes = {"/Named", "/GoTo"}
        user_gesture_inert_subtypes = inert_action_subtypes | {"/URI", "/Hide"}
        inert_named_destinations = {
            "/NextPage",
            "/PrevPage",
            "/FirstPage",
            "/LastPage",
            "/GoBack",
            "/GoForward",
        }

        def is_internal_destination(destination: object) -> bool:
            """A /GoTo /D must be a name/string (Dests-tree lookup) or an
            explicit [page, fit-mode, ...] array pointing at a page in this
            document. Never accept destinations shaped like remote/embedded
            file targets (those use /GoToR //GoToE with their own /F, which
            never reach this function under subtype /GoTo)."""
            if isinstance(destination, IndirectObject):
                destination = destination.get_object()
            if isinstance(destination, str):
                return True
            if isinstance(destination, ArrayObject):
                if len(destination) < 2:
                    return False
                page_ref = destination[0]
                page_target = (
                    page_ref.get_object() if isinstance(page_ref, IndirectObject) else page_ref
                )
                return isinstance(page_target, DictionaryObject) and str(
                    page_target.get("/Type", "")
                ) in {"/Page", "/Pages"}
            return False

        def is_internal_hide_target(target: object) -> bool:
            """A /Hide /T names this document's own field(s)/annotation(s)
            (PDF 1.7 Table 8.60): a text field-name string, an annotation
            dictionary reference, or an array mixing those. An annotation
            dictionary is validated by its own /Type /Annot (its /F is the
            integer annotation-flags entry, NOT an external file specification,
            so /F is legal here); a file-specification-shaped dict or any
            forbidden construct is rejected. Nothing here is ever resolved,
            fetched, or executed."""
            if isinstance(target, IndirectObject):
                target = target.get_object()
            if isinstance(target, str):
                return True
            if isinstance(target, DictionaryObject):
                if forbidden.intersection(target.keys()):
                    return False
                # Only a genuine annotation dictionary is an acceptable object
                # target; /FS or /EF mark a file specification, never a hide
                # target, and an /F without /Type /Annot is treated as a
                # file-spec shape rather than annotation flags.
                if {"/FS", "/EF"}.intersection(target.keys()):
                    return False
                if str(target.get("/Type", "")) != "/Annot":
                    return False
                return True
            if isinstance(target, ArrayObject):
                return len(target) > 0 and all(is_internal_hide_target(entry) for entry in target)
            return False

        def reject_dangerous_action_chain(
            action: object,
            *,
            depth: int = 0,
            chain_seen: set[int] | None = None,
            allowed_subtypes: frozenset[str] = frozenset(inert_action_subtypes),
            allow_user_named_action: bool = False,
        ) -> None:
            """Validate one action dict/array fail-closed; recurse only into its
            own /Next chain (never treat a raw /OpenAction destination array as
            an action array -- see the caller, which only reaches here with true
            action dicts/action-arrays already distinguished from destination
            arrays).

            Anything not in `allowed_subtypes` is rejected, including
            missing/unknown /S, dangerous /S, or malformed /Next links. Hidden
            or unreachable placement (e.g. inside /AA rather than the page tree)
            does not exempt an action from this check. `chain_seen` bounds
            recursive work on shared/cyclic /Next graphs by unique node
            identity, independent of the `depth` cap for pathologically long
            linear chains.
            """
            if chain_seen is None:
                chain_seen = set()
            if depth > 32:  # bounded traversal of pathological /Next chains
                raise UploadRejected("PDF_INVALID")
            if isinstance(action, IndirectObject):
                action = action.get_object()
            if isinstance(action, ArrayObject):
                for item in action:
                    reject_dangerous_action_chain(
                        item,
                        depth=depth + 1,
                        chain_seen=chain_seen,
                        allowed_subtypes=allowed_subtypes,
                        allow_user_named_action=allow_user_named_action,
                    )
                return
            if not isinstance(action, DictionaryObject):
                raise UploadRejected("PDF_INVALID")
            if id(action) in chain_seen:
                return  # already validated via another /Next reference; do not re-walk
            chain_seen.add(id(action))
            subtype = action.get("/S")
            subtype_name = str(subtype) if subtype is not None else ""
            if subtype_name not in allowed_subtypes:
                raise UploadRejected("PDF_INVALID")
            if forbidden.intersection(action.keys()):
                raise UploadRejected("PDF_INVALID")
            if subtype_name == "/Named":
                destination = action.get("/N")
                # Print/Find open viewer UI only on a user gesture, never automatically.
                if str(destination) not in inert_named_destinations and not (
                    allow_user_named_action and str(destination) in {"/Print", "/Find"}
                ):
                    raise UploadRejected("PDF_INVALID")
            elif subtype_name == "/GoTo":
                # /GoTo targets an internal destination only; /GoToR and /GoToE
                # (remote/embedded file targets) remain unconditionally forbidden
                # above and can never reach this branch under this subtype name.
                if "/D" not in action or not is_internal_destination(action.get("/D")):
                    raise UploadRejected("PDF_INVALID")
            elif subtype_name == "/URI" and "/URI" not in action:
                raise UploadRejected("PDF_INVALID")
            elif subtype_name == "/Hide":
                # /Hide only toggles visibility of this document's own fields
                # /annotations and is permitted solely under a user gesture (it
                # is absent from inert_action_subtypes, so an automatic
                # /OpenAction or automatic /AA event carrying /Hide is rejected
                # before reaching here). Its /T must name an annotation dict, a
                # text field-name string, or an array of those; a /Hide with no
                # /T is a malformed no-op and a /T shaped like a file
                # specification is not a legitimate hide target. Its optional
                # /H, when present, is a boolean. No target is ever resolved,
                # fetched, or executed by this checker.
                hidden_flag = action.get("/H")
                if hidden_flag is not None and not isinstance(hidden_flag, BooleanObject):
                    raise UploadRejected("PDF_INVALID")
                if "/T" not in action or not is_internal_hide_target(action.get("/T")):
                    raise UploadRejected("PDF_INVALID")
            if "/Next" in action:
                reject_dangerous_action_chain(
                    action.get("/Next"),
                    depth=depth + 1,
                    chain_seen=chain_seen,
                    allowed_subtypes=allowed_subtypes,
                    allow_user_named_action=allow_user_named_action,
                )

        def reject_dangerous_open_action(open_action: object, *, allow_uri: bool = False) -> None:
            """/OpenAction (like a /GoTo /D) may be either an action dictionary
            or a bare destination array `[page fit-mode ...]` (PDF 32000-1
            12.3.2). A bare destination array must never be walked as if its
            elements were actions -- that misreads a legitimate direct
            destination as a malformed action list and rejects harmless files.
            /URI is only inert when reached via an annotation/outline's direct
            /A (`allow_uri=True`): that fires solely on an explicit user
            gesture, unlike /OpenAction which auto-fires on document open.
            """
            if isinstance(open_action, IndirectObject):
                open_action = open_action.get_object()
            if isinstance(open_action, ArrayObject):
                if not is_internal_destination(open_action):
                    raise UploadRejected("PDF_INVALID")
                return
            reject_dangerous_action_chain(
                open_action,
                allowed_subtypes=frozenset(user_gesture_inert_subtypes)
                if allow_uri
                else frozenset(inert_action_subtypes),
                allow_user_named_action=allow_uri,
            )

        while pending:
            value = pending.pop()
            if isinstance(value, IndirectObject):
                value = value.get_object()
            if isinstance(value, DictionaryObject | ArrayObject):
                if id(value) in seen:
                    # Already inspected via another reference (shared/fan-in
                    # nodes); do not recount or re-enqueue its children.
                    continue
                seen.add(id(value))
            if len(seen) > limits.max_objects:
                raise UploadRejected("UPLOAD_LIMIT_EXCEEDED")
            if isinstance(value, DictionaryObject):
                if forbidden.intersection(value.keys()) or str(value.get("/S", "")) in forbidden:
                    raise UploadRejected("PDF_INVALID")
                if str(value.get("/Type", "")) == "/EmbeddedFile":
                    raise UploadRejected("PDF_INVALID")
                # A free-floating action dict (/Type /Action) reachable only via
                # xref/objStm indexing -- not necessarily via /AA, /OpenAction, or
                # /A -- must still be classified fail-closed even if nothing
                # currently points to it (hidden/unreachable active actions).
                # The xref scan has no trigger context. URI/Print may be stored
                # here, but every actual reference is rechecked below: automatic
                # triggers remain strict regardless of shared-object scan order.
                if str(value.get("/Type", "")) == "/Action":
                    reject_dangerous_action_chain(
                        value,
                        allowed_subtypes=frozenset(user_gesture_inert_subtypes),
                        allow_user_named_action=True,
                    )
                for action_key in action_table_keys.intersection(value.keys()):
                    action_table = value.get(action_key)
                    if isinstance(action_table, IndirectObject):
                        action_table = action_table.get_object()
                    if isinstance(action_table, DictionaryObject):
                        for trigger, trigger_action in action_table.items():
                            # ISO 32000-1 Table 194: Widget mouse down/up require
                            # a click; focus/page-open and document triggers do not.
                            gesture = (
                                str(value.get("/Type", "")) == "/Annot"
                                and str(value.get("/Subtype", "")) == "/Widget"
                                and str(trigger) in {"/D", "/U"}
                            )
                            reject_dangerous_action_chain(
                                trigger_action,
                                allowed_subtypes=frozenset(user_gesture_inert_subtypes)
                                if gesture
                                else frozenset(inert_action_subtypes),
                                allow_user_named_action=gesture,
                            )
                    else:
                        raise UploadRejected("PDF_INVALID")
                for action_key in direct_action_keys.intersection(value.keys()):
                    target = value.get(action_key)
                    resolved = target.get_object() if isinstance(target, IndirectObject) else target
                    if action_key == "/A" and not (
                        isinstance(resolved, DictionaryObject) and "/S" in resolved
                    ):
                        continue  # structure-element attributes, not an action
                    reject_dangerous_open_action(target, allow_uri=action_key == "/A")
                if isinstance(value, StreamObject):
                    if "/F" in value:  # External stream file; never resolve/fetch it.
                        raise UploadRejected("PDF_INVALID")
                    filters = value.get("/Filter", [])
                    if not isinstance(filters, list):
                        filters = [filters]
                    allowed = {
                        "/FlateDecode",
                        "/ASCIIHexDecode",
                        "/ASCII85Decode",
                        "/LZWDecode",
                        "/RunLengthDecode",
                        "/DCTDecode",
                        "/JPXDecode",
                        "/CCITTFaxDecode",
                    }
                    if any(str(item) not in allowed for item in filters):
                        raise UploadRejected("PDF_INVALID")
                    decoded += len(value.get_data())
                    if decoded > limits.max_decoded_bytes:
                        raise UploadRejected("UPLOAD_LIMIT_EXCEEDED")
                pending.extend(value.values())
            elif isinstance(value, ArrayObject):
                pending.extend(value)
        return pages


if __name__ == "__main__":
    import logging

    logging.disable(logging.CRITICAL)
    try:
        pages = _inspect(sys.argv[1], PdfLimits(**json.loads(sys.argv[2])))
        print(json.dumps({"code": "verified", "pages": pages}))
    except MemoryError:
        print('{"code":"UPLOAD_LIMIT_EXCEEDED"}')
    except Exception as error:
        from pypdf.errors import LimitReachedError

        code = (
            "UPLOAD_LIMIT_EXCEEDED"
            if isinstance(error, MemoryError | LimitReachedError)
            else str(error)
            if isinstance(error, UploadRejected)
            else "PDF_INVALID"
        )
        print(json.dumps({"code": code}))
