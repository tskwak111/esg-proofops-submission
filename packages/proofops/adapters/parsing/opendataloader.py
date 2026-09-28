"""Actual OpenDataLoader + pdfplumber parsing in a bounded, no-model subprocess.

Local immutable filesystem artifacts are synthetic storage, not an S3 adapter.
The parsers themselves really execute. No hybrid/OCR/vision endpoint is invoked.
"""

from __future__ import annotations

import json
import math
import os
import re
import shutil
import subprocess
import sys
from contextlib import contextmanager
from dataclasses import asdict, replace
from hashlib import sha256
from importlib.metadata import version
from pathlib import Path
from tempfile import TemporaryDirectory
from time import monotonic, sleep
from typing import Any
from uuid import UUID, uuid4, uuid5

from proofops.adapters.parsing.windows_isolation import (
    IsolationUnavailable,
    kill_and_reap,
    process_isolation,
)
from proofops.application.ingest.geometry import affine_apply, invert_affine
from proofops.application.ingest.graph_fusion import (
    CandidateBatch,
    CandidateBlock,
    CandidateEdge,
    CanonicalDocumentGraph,
    ParserProfile,
    QualityIssue,
    SourceArtifact,
    candidates_from_snapshot,
    fuse_candidates,
)
from proofops.application.ports.parsing import ParserPort
from proofops.application.uploads_security import PdfLimits, QuarantinedPdf, verify_quarantined_pdf
from proofops.domain.documents import NativeSource, PageGeometry, _compose_affine
from proofops.domain.errors import DomainValidationError


class ParseFailure(ValueError):
    """Sanitized error code only; no parser stderr, PDF text, or private path."""


def _table_verifier(policy_hash):
    from proofops.adapters.local import table_source_verification as legacy

    if policy_hash == legacy.policy_sha256():
        return legacy
    from proofops.adapters.local import merged_table_verification as merged

    if policy_hash == merged.policy_sha256():
        return merged
    from proofops.adapters.local import selected_cell_table_verification as selected

    if policy_hash == selected.policy_sha256():
        return selected
    raise ParseFailure("TABLE_SOURCE_POLICY_MISMATCH")


def _locates_auxiliary_cells(profile):
    """Only the selected-cell policy asks for ink-tight auxiliary cell boxes.

    Pinning that policy in the parser profile IS the new source version: existing
    runs keep their own policy and their artifacts are never rewritten.
    """
    policy_hash = (
        profile.get("table_source_policy_sha256")
        if isinstance(profile, dict)
        else profile.table_source_policy_sha256
    )
    if policy_hash is None:
        return False
    from proofops.adapters.local import selected_cell_table_verification as selected

    return policy_hash == selected.policy_sha256()


def _json(value: object) -> bytes:
    return json.dumps(
        value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False
    ).encode()


def _transform_box(box, matrix):
    if box is None:
        return None
    if (
        not isinstance(box, list | tuple)
        or len(box) != 4
        or any(
            isinstance(x, bool) or not isinstance(x, int | float) or not math.isfinite(x)
            for x in box
        )
        or box[0] >= box[2]
        or box[1] >= box[3]
    ):
        raise ParseFailure("PARSER_GEOMETRY_INVALID")
    points = [affine_apply(matrix, x, y) for x in (box[0], box[2]) for y in (box[1], box[3])]
    return (
        min(p[0] for p in points),
        min(p[1] for p in points),
        max(p[0] for p in points),
        max(p[1] for p in points),
    )


def _geometry(value: dict) -> PageGeometry:
    return PageGeometry(
        value["width_pt"], value["height_pt"], value["rotation"], tuple(value["crop_box"])
    )


def _node_text(node: dict, depth=0) -> str:
    if depth > 100:
        raise ParseFailure("PARSER_OUTPUT_LIMIT")
    if "content" in node:
        if not isinstance(node["content"], str):
            raise ParseFailure("PARSER_SCHEMA_INVALID")
        return node["content"]
    key = "rows" if "rows" in node else "cells" if "cells" in node else "kids"
    children = node.get(key, [])
    if not isinstance(children, list) or any(not isinstance(child, dict) for child in children):
        raise ParseFailure("PARSER_SCHEMA_INVALID")
    return ("\t" if key == "cells" else "\n").join(
        _node_text(child, depth + 1) for child in children
    )


def _batch(
    raw: dict,
    geometries: dict,
    source: SourceArtifact,
    profile: ParserProfile,
    config_hash: str,
    *,
    auxiliary=False,
) -> CandidateBatch:
    run_id = str(uuid4())
    blocks: list[CandidateBlock] = []
    edges: list[CandidateEdge] = []
    if not isinstance(raw.get("kids"), list):
        raise ParseFailure("PARSER_SCHEMA_INVALID")
    stack: list[tuple[dict[str, Any], str | None, int | None, str | None, int]] = [
        (node, None, None, None, 0) for node in reversed(raw["kids"])
    ]
    while stack:
        node, parent, inherited_page, table_id, depth = stack.pop()
        if not isinstance(node, dict) or depth > 100 or len(blocks) >= 100000:
            raise ParseFailure("PARSER_OUTPUT_LIMIT")
        native_id = str(node["id"])
        page = node.get("page number", inherited_page)
        if type(page) is not int or str(page) not in geometries:
            raise ParseFailure("PARSER_PAGE_MAPPING_INVALID")
        info = geometries[str(page)]
        geometry = _geometry(info["crop"])
        kind = {
            "table row": "table_row",
            "table cell": "table_cell",
            "image": "figure",
            "list item": "paragraph",
        }.get(node["type"], node["type"])
        if kind not in (
            "paragraph",
            "heading",
            "table",
            "table_row",
            "table_cell",
            "caption",
            "footnote",
            "figure",
            "unknown",
        ):
            kind = "unknown"
        if kind == "table":
            table_id = native_id
        raw_bbox = node.get("bounding box")
        if auxiliary:
            media = _geometry(info["media"])
            origin = node["page origin"]
            shift = (1.0, 0.0, 0.0, 1.0, -origin[0], -origin[1])
            to_native = _compose_affine(invert_affine(media.to_canonical_affine()), shift)
            to_canonical = _compose_affine(geometry.to_canonical_affine(), to_native)
            coordinate_system = "pdfplumber_top_left_media_points"
        else:
            # Actual OD 2.5.7 output is already rotation/CropBox-relative bottom-left.
            to_canonical = (1.0, 0.0, 0.0, -1.0, 0.0, geometry.height_pt)
            to_native = _compose_affine(invert_affine(geometry.to_canonical_affine()), to_canonical)
            coordinate_system = "opendataloader_display_bottom_left_points"
        native_bbox = _transform_box(raw_bbox, to_native)
        text = _node_text(node)
        native = NativeSource(
            source.document_version_id,
            profile.parse_manifest_id,
            run_id,
            native_id,
            page,
            info.get("printed_page_label"),
            native_bbox,
            "pdf_bottom_left_points",
            text,
            0,
            len(text),
        )
        context = tuple(
            f"{key}={node[key]}" for key in ("row number", "column number") if key in node
        )
        blocks.append(
            CandidateBlock(
                kind,
                native,
                geometry,
                context,
                tuple(raw_bbox) if raw_bbox is not None else None,
                coordinate_system,
                to_canonical,
                table_id,
                node.get("row number"),
                node.get("column number"),
                node.get("row span"),
                node.get("column span"),
            )
        )
        if parent is not None:
            edges.append(
                CandidateEdge(native_id, parent, "table_parent" if table_id else "section_parent")
            )
        # OD uses a separate key for list children, including misclassified prose continuations.
        children = [
            child for key in ("kids", "rows", "cells", "list items") for child in node.get(key, [])
        ]
        for child in reversed(children):
            stack.append((child, native_id, page, table_id, depth + 1))
    name = "pdfplumber" if auxiliary else "opendataloader"
    return CandidateBatch(
        source.tenant_id,
        source.document_version_id,
        profile.parse_manifest_id,
        source.sha256,
        run_id,
        name,
        version("pdfplumber" if auxiliary else "opendataloader-pdf"),
        "pdfminer" if auxiliary else "opendataloader",
        config_hash,
        tuple(blocks),
        tuple(edges),
        synthetic=False,
    )


def _with_quality_issues(graph, selected, profile):
    issues = list(graph.issues)
    for page in selected:
        if not any(
            block.page_num == page and any(s.raw_text for s in block.sources)
            for block in graph.blocks
        ):
            issues.append(
                QualityIssue(
                    str(uuid5(UUID(profile.parse_manifest_id), f"unreadable:{page}")),
                    "no_extractable_text",
                    page,
                    (),
                    "unreadable",
                    "No text extracted; absence of evidence is not established.",
                )
            )
    for block in graph.blocks:
        if block.kind == "table":
            issues.append(
                QualityIssue(
                    str(uuid5(UUID(block.source_id), "vision-not-run")),
                    "table_vision_not_run",
                    block.page_num,
                    (block.source_id,),
                    "open",
                    "Vision cross-check not_run: approved runtime/account required.",
                )
            )
    return replace(graph, issues=tuple(issues))


class OpenDataLoaderParser(ParserPort):
    def __init__(self, artifact_root: Path):
        self.artifact_root = Path(artifact_root)

    def parse(
        self, source: SourceArtifact, profile: ParserProfile, *, tenant_id: str
    ) -> CanonicalDocumentGraph:
        if source.tenant_id != tenant_id:
            raise ParseFailure("NOT_FOUND")
        if len(source.content) > profile.max_bytes:
            raise ParseFailure("PARSER_INPUT_LIMIT")
        if sha256(source.content).hexdigest() != source.sha256:
            raise ParseFailure("SOURCE_INTEGRITY_MISMATCH")
        if version("opendataloader-pdf") != "2.5.7":
            raise ParseFailure("PARSER_VERSION_UNSUPPORTED")
        final = (
            self.artifact_root / tenant_id / source.document_version_id / profile.parse_manifest_id
        )
        if final.exists():
            raise ParseFailure("PARSE_MANIFEST_EXISTS")
        java = shutil.which(profile.java_executable)
        if not java:
            raise ParseFailure("JAVA_21_REQUIRED")
        try:
            runtime = subprocess.run(
                [java, "-version"], capture_output=True, timeout=profile.timeout_seconds, check=True
            ).stderr.decode(errors="replace")
        except (OSError, subprocess.SubprocessError):
            raise ParseFailure("JAVA_21_REQUIRED") from None
        if re.search(r'version "21\.', runtime) is None:
            raise ParseFailure("JAVA_21_REQUIRED")
        verified = verify_quarantined_pdf(
            QuarantinedPdf(
                tenant_id,
                source.document_version_id,
                source.object_version_id,
                source.content,
                len(source.content),
                source.sha256,
            ),
            PdfLimits(max_bytes=profile.max_bytes, timeout_seconds=profile.timeout_seconds),
            tenant_id=tenant_id,
        )
        selected = profile.physical_pages or tuple(range(1, verified.page_count + 1))
        if any(page > verified.page_count for page in selected):
            raise ParseFailure("PAGE_SELECTION_INVALID")
        profile_hash = sha256(_json(profile.invocation_snapshot())).hexdigest()
        final.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        with _parser_work_directory(final.parent) as work:
            (work / "source.pdf").write_bytes(source.content)
            (work / "request.json").write_bytes(
                _json(dict(profile=profile.invocation_snapshot(), selected=selected))
            )
            env = _child_environment(java, work, profile)
            command = [sys.executable, "-I", str(Path(__file__).resolve()), str(work)]
            self._execute(command, work, env, profile)
            try:
                geometries = json.loads((work / "geometry.json").read_bytes())
                raw = json.loads((work / "source.json").read_bytes())
                if raw.get("number of pages") != verified.page_count:
                    raise ParseFailure("PARSER_PAGE_MAPPING_INVALID")
                if profile.table_structure_repair is not None:
                    raw = json.loads((work / "source-repaired.json").read_bytes())
                candidates = [_batch(raw, geometries, source, profile, profile_hash)]
                if profile.table_auxiliary:
                    auxiliary = json.loads((work / "auxiliary.json").read_bytes())
                    candidates.append(
                        _batch(auxiliary, geometries, source, profile, profile_hash, auxiliary=True)
                    )
                fusion_version = 4 if profile.table_structure_repair == "odl_header_v2" else 3
                graph = fuse_candidates(
                    tuple(candidates), tenant_id=tenant_id, fusion_version=fusion_version
                )
            except DomainValidationError:
                raise ParseFailure("PARSER_GEOMETRY_INVALID") from None
            except (KeyError, TypeError, json.JSONDecodeError):
                raise ParseFailure("PARSER_SCHEMA_INVALID") from None
            graph = _with_quality_issues(graph, selected, profile)
            (work / "graph.json").write_bytes(_json(graph.to_dict()))
            (work / "quality.json").write_bytes(_json([issue.to_dict() for issue in graph.issues]))
            (work / "candidates.json").write_bytes(
                _json([asdict(batch) for batch in graph.candidates])
            )
            returned_graph = graph
            if profile.table_source_policy_sha256 is not None:
                verifier = _table_verifier(profile.table_source_policy_sha256)
                receipt = verifier.attest_tables(graph, source.content, tenant_id=tenant_id)
                (work / "table-source.json").write_bytes(_json(receipt))
                returned_graph = verifier.replay_tables(
                    receipt, graph, source.content, tenant_id=tenant_id
                )
            (work / "source.pdf").unlink()
            (work / "request.json").unlink()
            artifacts = {
                path.name: sha256(path.read_bytes()).hexdigest()
                for path in work.iterdir()
                if path.is_file()
            }
            from importlib.resources import files

            jar = files("opendataloader_pdf").joinpath("jar", "opendataloader-pdf-cli.jar")
            manifest = dict(
                schema_version="1",
                fusion_version=fusion_version,
                tenant_id=tenant_id,
                document_id=source.document_id,
                document_version_id=source.document_version_id,
                parse_manifest_id=profile.parse_manifest_id,
                source_sha256=source.sha256,
                object_version_id=source.object_version_id,
                synthetic_source=source.synthetic,
                local_synthetic_storage=True,
                parser_version="2.5.7",
                parser_profile_sha256=profile_hash,
                parser_config_sha256=profile.config_hash(),
                parser_config=profile.config_snapshot(),
                parser_profile=profile.invocation_snapshot(),
                java_version=runtime.splitlines()[0],
                jar_sha256=sha256(jar.read_bytes()).hexdigest(),
                selected_physical_pages=selected,
                validation_profile="fast_preview",
                vision_status="not_run",
                artifacts=artifacts,
                parser_runs=[
                    dict(
                        parser_run_id=b.parser_run_id,
                        parser_name=b.parser_name,
                        parser_version=b.parser_version,
                        parser_family=b.parser_family,
                        config_hash=b.config_hash,
                    )
                    for b in graph.candidates
                ],
            )
            (work / "manifest.json").write_bytes(_json(manifest))
            if sum(path.stat().st_size for path in work.iterdir()) > profile.max_output_bytes:
                raise ParseFailure("PARSER_OUTPUT_LIMIT")
            for path in work.iterdir():
                path.chmod(0o400)
            try:
                os.rename(work, final)
            except OSError:
                raise ParseFailure("PARSE_MANIFEST_EXISTS") from None
            return returned_graph

    def load_verified(
        self,
        source: SourceArtifact,
        profile: ParserProfile,
        *,
        tenant_id: str,
        manifest_sha256: str | None = None,
    ) -> CanonicalDocumentGraph:
        """Reuse immutable output only after identity, config and every artifact hash agree."""
        if source.tenant_id != tenant_id:
            raise ParseFailure("NOT_FOUND")
        if sha256(source.content).hexdigest() != source.sha256:
            raise ParseFailure("SOURCE_INTEGRITY_MISMATCH")
        directory = (
            self.artifact_root / tenant_id / source.document_version_id / profile.parse_manifest_id
        )
        try:
            if directory.is_symlink():
                raise ValueError("symlink")
            manifest_path = directory / "manifest.json"
            if (
                manifest_path.is_symlink()
                or manifest_path.stat().st_size > profile.max_output_bytes
            ):
                raise ValueError("unsafe manifest")
            raw_manifest = manifest_path.read_bytes()
            if manifest_sha256 is not None and sha256(raw_manifest).hexdigest() != manifest_sha256:
                raise ValueError("manifest hash")
            manifest = json.loads(raw_manifest)
            expected = dict(
                schema_version="1",
                tenant_id=tenant_id,
                document_id=source.document_id,
                document_version_id=source.document_version_id,
                parse_manifest_id=profile.parse_manifest_id,
                source_sha256=source.sha256,
                object_version_id=source.object_version_id,
                synthetic_source=source.synthetic,
                local_synthetic_storage=True,
                parser_version="2.5.7",
                parser_profile_sha256=sha256(_json(profile.invocation_snapshot())).hexdigest(),
                parser_profile=json.loads(_json(profile.invocation_snapshot())),
                parser_config_sha256=profile.config_hash(),
                parser_config=profile.config_snapshot(),
                validation_profile="fast_preview",
                vision_status="not_run",
            )
            if any(manifest.get(key) != value for key, value in expected.items()):
                raise ValueError("manifest identity")
            artifacts = manifest["artifacts"]
            required = {
                "graph.json",
                "quality.json",
                "candidates.json",
                "geometry.json",
                "source.json",
                "source.md",
            }
            if profile.table_auxiliary:
                required.add("auxiliary.json")
            if profile.table_structure_repair is not None:
                required.update({"source-repaired.json", "table-repair.json"})
            elif {"source-repaired.json", "table-repair.json"} & artifacts.keys():
                raise ValueError("unconfigured table repair")
            if profile.table_source_policy_sha256 is not None:
                required.add("table-source.json")
            elif "table-source.json" in artifacts:
                raise ValueError("unconfigured table attestation")
            if not required <= artifacts.keys():
                raise ValueError("missing artifacts")
            values = {}
            total = len(raw_manifest)
            for name, digest in artifacts.items():
                path = directory / name
                if Path(name).name != name or path.is_symlink():
                    raise ValueError("unsafe artifact path")
                total += path.stat().st_size
                if total > profile.max_output_bytes:
                    raise ValueError("artifact limit")
                data = path.read_bytes()
                if sha256(data).hexdigest() != digest:
                    raise ValueError("artifact hash")
                if name in {"graph.json", "quality.json", "candidates.json", "table-source.json"}:
                    values[name] = json.loads(data)
            candidates = candidates_from_snapshot(values["candidates.json"])
            # Existing immutable manifests predate fusion versioning.
            fusion_version = manifest.get("fusion_version", 1)
            if type(fusion_version) is not int or fusion_version not in (1, 2, 3, 4):
                raise ValueError("unsupported fusion version")
            graph = fuse_candidates(candidates, tenant_id=tenant_id, fusion_version=fusion_version)
            if graph.to_dict() != values["graph.json"] or (
                graph.document_version_id,
                graph.parse_manifest_id,
                graph.source_sha256,
            ) != (source.document_version_id, profile.parse_manifest_id, source.sha256):
                raise ValueError("graph identity")
            if any(batch.config_hash != expected["parser_profile_sha256"] for batch in candidates):
                raise ValueError("candidate configuration")
            selected = manifest["selected_physical_pages"]
            if profile.physical_pages and selected != list(profile.physical_pages):
                raise ValueError("page selection")
            if any(block.page_num not in selected for block in graph.blocks):
                raise ValueError("unselected block")
            issues = tuple(
                QualityIssue(**{**issue, "source_ids": tuple(issue["source_ids"])})
                for issue in values["quality.json"]
            )
            restored = _with_quality_issues(graph, selected, profile)
            if restored.issues != issues:
                raise ValueError("quality issue mismatch")
            from importlib.resources import files

            jar = files("opendataloader_pdf").joinpath("jar", "opendataloader-pdf-cli.jar")
            if manifest["jar_sha256"] != sha256(jar.read_bytes()).hexdigest():
                raise ValueError("parser binary mismatch")
            expected_parsers = {"opendataloader": ("2.5.7", "opendataloader")}
            if profile.table_auxiliary:
                expected_parsers["pdfplumber"] = (version("pdfplumber"), "pdfminer")
            if {
                batch.parser_name: (batch.parser_version, batch.parser_family)
                for batch in candidates
            } != expected_parsers or any(batch.synthetic for batch in candidates):
                raise ValueError("parser identity mismatch")
            if manifest["parser_runs"] != [
                dict(
                    parser_run_id=b.parser_run_id,
                    parser_name=b.parser_name,
                    parser_version=b.parser_version,
                    parser_family=b.parser_family,
                    config_hash=b.config_hash,
                )
                for b in candidates
            ]:
                raise ValueError("parser run mismatch")
            if profile.table_source_policy_sha256 is not None:
                verifier = _table_verifier(profile.table_source_policy_sha256)
                restored = verifier.replay_tables(
                    values["table-source.json"], restored, source.content, tenant_id=tenant_id
                )
            return restored
        except (OSError, ValueError, KeyError, TypeError):
            raise ParseFailure("PARSER_ARTIFACT_INTEGRITY_MISMATCH") from None

    @staticmethod
    def _execute(command, work, env, profile):
        deadline = monotonic() + profile.timeout_seconds
        cpu_seconds = max(1, math.ceil(profile.timeout_seconds))
        try:
            isolation = process_isolation(
                memory_bytes=profile.memory_bytes, cpu_seconds=cpu_seconds
            )
        except IsolationUnavailable:
            # Containment is not optional: refuse to parse rather than run loose.
            raise ParseFailure("PARSER_ISOLATION_UNAVAILABLE") from None
        try:
            with isolation.spawn(command, cwd=work, env=env) as process:
                try:
                    while process.poll() is None:
                        if monotonic() >= deadline:
                            raise ParseFailure("PARSER_TIMEOUT")
                        _check_limits(isolation, process, work, profile)
                        try:
                            process.wait(timeout=min(0.05, max(0.001, deadline - monotonic())))
                        except subprocess.TimeoutExpired:
                            pass
                    # A child can allocate or write past a limit and exit between
                    # two samples; the breach is still a breach, so sample once
                    # more now that the outcome is final.
                    _check_limits(isolation, process, work, profile)
                    if process.returncode:
                        raise ParseFailure("PARSER_FAILED")
                finally:
                    try:
                        isolation.terminate_tree(process)
                    finally:
                        kill_and_reap(process)
        except IsolationUnavailable:
            raise ParseFailure("PARSER_ISOLATION_UNAVAILABLE") from None
        finally:
            isolation.close()


def _cleanup_parser_work(temporary, parent: Path, *, timeout_seconds: float = 15) -> None:
    deadline = monotonic() + timeout_seconds
    parent = parent.resolve()
    while True:
        target = Path(temporary.name).resolve()
        if target.parent != parent or not target.name.startswith(".parse-"):
            raise ValueError("parser cleanup target outside its artifact parent")
        try:
            temporary.cleanup()
            return
        except PermissionError:
            # Job termination is asynchronous. Windows may retain a descendant's
            # file handles briefly even after the job's active count reaches zero.
            # Retry only this owned scratch directory, with a fixed deadline.
            if sys.platform != "win32" or monotonic() >= deadline:
                raise
            sleep(0.01)


@contextmanager
def _parser_work_directory(parent: Path):
    temporary = TemporaryDirectory(prefix=".parse-", dir=parent)
    try:
        yield Path(temporary.name)
    finally:
        _cleanup_parser_work(temporary, parent)


def _output_bytes(work: Path) -> int:
    import stat

    total = 0
    for path in work.iterdir():
        if path.name == "source.pdf":
            continue
        try:
            metadata = path.stat()
        except FileNotFoundError:
            # The JVM can remove a temporary PDF between enumeration and stat.
            # A removed file consumes no output bytes; other I/O failures remain
            # errors rather than silently disabling the limit.
            continue
        if stat.S_ISREG(metadata.st_mode):
            total += metadata.st_size
    return total


def _check_limits(isolation, process, work: Path, profile) -> None:
    if _output_bytes(work) > profile.max_output_bytes:
        raise ParseFailure("PARSER_OUTPUT_LIMIT")
    if isolation.memory_limit_exceeded(process):
        raise ParseFailure("PARSER_MEMORY_LIMIT")


def _child_limits(profile: dict[str, Any]) -> None:
    """Self-imposed limits inside the launcher.

    POSIX keeps the original hard rlimits. On Windows the same CPU and memory
    ceilings are already enforced by the job object the parent assigned before
    this process was resumed, so the only thing left to do here is suppress the
    crash dialog that would otherwise hold a terminated parse open forever;
    `RLIMIT_FSIZE` has no Windows analogue and the parent's output watchdog is
    what bounds written bytes on both platforms.
    """
    cpu = max(1, math.ceil(profile["timeout_seconds"]))
    if sys.platform != "win32":
        import resource

        resource.setrlimit(resource.RLIMIT_CPU, (cpu, cpu))
        resource.setrlimit(
            resource.RLIMIT_FSIZE, (profile["max_output_bytes"], profile["max_output_bytes"])
        )
        resource.setrlimit(resource.RLIMIT_CORE, (0, 0))
        return
    import ctypes

    # SEM_FAILCRITICALERRORS | SEM_NOGPFAULTERRORBOX | SEM_NOOPENFILEERRORBOX
    ctypes.WinDLL("kernel32", use_last_error=True).SetErrorMode(0x0001 | 0x0002 | 0x8000)


def _child_environment(java: str, work: Path, profile) -> dict[str, str]:
    """The minimal environment the launcher and its JVM need, and nothing else.

    The inherited environment is never passed through on either platform. On
    Windows a JVM additionally needs `SystemRoot` and the system directory to
    resolve core DLLs, and the temporary directory is pinned inside the
    per-parse work directory so scratch files stay under the output watchdog.
    """
    heap = f"-Xmx{max(1, profile.memory_bytes // (2 * 1024 * 1024))}m"
    if sys.platform != "win32":
        return {
            "PATH": str(Path(java).parent) + ":/usr/bin:/bin",
            "JAVA_TOOL_OPTIONS": heap,
            "PYTHONDONTWRITEBYTECODE": "1",
        }
    system_root = os.environ.get("SystemRoot", r"C:\Windows")
    system32 = str(Path(system_root) / "System32")
    return {
        "PATH": os.pathsep.join([str(Path(java).parent), system32, system_root]),
        "SystemRoot": system_root,
        "SystemDrive": os.environ.get("SystemDrive", "C:"),
        "ComSpec": os.environ.get("ComSpec", str(Path(system32) / "cmd.exe")),
        "NUMBER_OF_PROCESSORS": os.environ.get("NUMBER_OF_PROCESSORS", "1"),
        "TEMP": str(work),
        "TMP": str(work),
        "JAVA_TOOL_OPTIONS": heap,
        "PYTHONDONTWRITEBYTECODE": "1",
        "PYTHONUTF8": "1",
    }


def _child(work: Path) -> None:
    from pypdf import PdfReader

    request = json.loads((work / "request.json").read_bytes())
    profile, selected = request["profile"], request["selected"]
    _child_limits(profile)
    reader = PdfReader(work / "source.pdf", strict=True)
    geometries = {}
    for number in selected:
        page = reader.pages[number - 1]
        rotation = int(page.rotation) % 360

        def box_geometry(box):
            x0, y0, x1, y1 = map(float, box)
            width, height = x1 - x0, y1 - y0
            if rotation in (90, 270):
                width, height = height, width
            return asdict(PageGeometry(width, height, rotation, (x0, y0, x1, y1)))

        geometries[str(number)] = dict(
            crop=box_geometry(page.cropbox),
            media=box_geometry(page.mediabox),
            printed_page_label=reader.page_labels[number - 1],
        )
    (work / "geometry.json").write_bytes(_json(geometries))
    import opendataloader_pdf

    opendataloader_pdf.convert(
        input_path=[str(work / "source.pdf")],
        output_dir=str(work),
        format="json,markdown",
        pages=",".join(map(str, selected)),
        image_output="off",
        hybrid="off",
        quiet=True,
    )
    if profile.get("table_structure_repair") in ("odl_header_v1", "odl_header_v2"):
        import pdfplumber
        from proofops.adapters.parsing.odl_table_repair import repair_stacked_headers

        # Unsupported page transforms remain untouched, never guessed.
        words_by_page = {}
        with pdfplumber.open(work / "source.pdf") as document:
            for number in selected:
                info = geometries[str(number)]
                crop, media = info["crop"], info["media"]
                if crop != media or crop["rotation"] or crop["crop_box"][:2] != (0, 0):
                    continue
                page = document.pages[number - 1]
                words_by_page[number] = [
                    {
                        "text": word["text"],
                        "bbox": [
                            word["x0"],
                            page.height - word["bottom"],
                            word["x1"],
                            page.height - word["top"],
                        ],
                    }
                    for word in page.extract_words()
                ]
        raw = json.loads((work / "source.json").read_bytes())
        repaired, receipts = repair_stacked_headers(
            raw, words_by_page, locate_rows=profile["table_structure_repair"] == "odl_header_v2"
        )
        (work / "source-repaired.json").write_bytes(_json(repaired))
        (work / "table-repair.json").write_bytes(
            _json(
                dict(
                    version=profile["table_structure_repair"],
                    source_json_sha256=sha256(_json(raw)).hexdigest(),
                    receipts=receipts,
                )
            )
        )
    if profile["table_auxiliary"]:
        import pdfplumber

        nodes = []
        with pdfplumber.open(work / "source.pdf") as document:
            for number in selected:
                page = document.pages[number - 1]
                for index, table in enumerate(page.find_tables()):
                    table_id = f"p{number}-t{index}"
                    options = {}
                    if profile.get("table_text_y_tolerance") is not None:
                        options["y_tolerance"] = profile["table_text_y_tolerance"]
                    rows, texts = [], table.extract(**options)
                    for row_index, row in enumerate(table.rows):
                        cells = []
                        for column_index, bbox in enumerate(row.cells):
                            if bbox is None:
                                continue
                            cells.append(
                                dict(
                                    type="table cell",
                                    id=f"{table_id}-r{row_index}-c{column_index}",
                                    **{
                                        "page number": number,
                                        "bounding box": bbox,
                                        "page origin": page.bbox[:2],
                                        "row number": row_index + 1,
                                        "column number": column_index + 1,
                                        "content": texts[row_index][column_index] or "",
                                    },
                                )
                            )
                        rows.append(
                            dict(
                                type="table row",
                                id=f"{table_id}-r{row_index}",
                                cells=cells,
                                **{
                                    "page number": number,
                                    "bounding box": row.bbox,
                                    "page origin": page.bbox[:2],
                                    "row number": row_index + 1,
                                },
                            )
                        )
                    nodes.append(
                        dict(
                            type="table",
                            id=table_id,
                            rows=rows,
                            **{
                                "page number": number,
                                "bounding box": table.bbox,
                                "page origin": page.bbox[:2],
                            },
                        )
                    )
        (work / "auxiliary.json").write_bytes(_json(dict(kids=nodes)))
        if _locates_auxiliary_cells(profile):
            from proofops.adapters.local.native_glyph_geometry import native_word_ink_geometry
            from proofops.adapters.parsing.odl_table_repair import locate_auxiliary_cells

            # pdfplumber assigns a slot's text by ink midpoint, so a detected slot
            # can hold text its own box does not contain. Snap each slot onto the
            # ink it owns; a table whose ownership is not exact stays untouched.
            words_by_page = {}
            source_pdf_bytes = (work / "source.pdf").read_bytes()
            with pdfplumber.open(work / "source.pdf") as document:
                for number in selected:
                    page = document.pages[number - 1]
                    raw_words = page.extract_words()
                    if not raw_words:
                        continue
                    word_indices = list(range(len(raw_words)))
                    try:
                        ink_res = native_word_ink_geometry(source_pdf_bytes, number, word_indices)
                        matched_map = {
                            m["native_word_index"]: m["ink_bbox"]
                            for m in ink_res.get("matched_words", [])
                        }
                        unresolved_set = set(ink_res.get("unresolved_word_indices", []))
                    except Exception:
                        matched_map = {}
                        unresolved_set = set(word_indices)
                    words_by_page[number] = [
                        {
                            "text": word["text"],
                            "bbox": [word["x0"], word["top"], word["x1"], word["bottom"]],
                            "ink_bbox": matched_map.get(idx),
                            "resolved": idx in matched_map and idx not in unresolved_set,
                        }
                        for idx, word in enumerate(raw_words)
                    ]
            located, receipts = locate_auxiliary_cells(
                nodes, words_by_page, source=source_pdf_bytes
            )
            (work / "auxiliary.json").write_bytes(_json(dict(kids=located, cell_locate=receipts)))


if __name__ == "__main__":
    _child(Path(sys.argv[1]))
