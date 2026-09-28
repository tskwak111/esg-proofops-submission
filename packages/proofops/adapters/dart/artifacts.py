"""Content-addressed immutable artifact storage and defensive file extraction.

Provides:
- ArtifactStore: Content-addressed atomic storage keyed by SHA256 with root escape checks
  and atomic exclusive no-overwrite publication.
- Safe zip extraction defending against path traversal (Zip Slip), symlink injection,
  resource exhaustion (Zip Bomb), and casefold collisions without overwriting existing files.
- Safe XML parser preventing XML External Entity (XXE) and entity expansion attacks,
  handling multi-byte, UTF-16, and declared-encoding XML DTD bypasses.
"""

from __future__ import annotations

import hashlib
import io
import os
import re
import uuid
import xml.etree.ElementTree as ET
import zipfile
from pathlib import Path

_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_SAFE_EXT_RE = re.compile(r"^\.[a-zA-Z0-9_.-]+$")


class DTDRejectingTreeBuilder(ET.TreeBuilder):
    """TreeBuilder that strictly forbids DOCTYPE declarations to prevent XXE attacks."""

    def doctype(self, name: str, pubid: str | None, system: str | None) -> None:
        raise ValueError("Forbidden DOCTYPE declaration detected in XML (XXE defense)")


def safe_parse_xml(content: bytes | str) -> ET.Element:
    """Parse XML string or bytes safely with multi-encoding XXE and DTD defense.

    Rejects any DOCTYPE or ENTITY declarations across all encodings (UTF-8, UTF-16, UTF-32,
    declared encodings) using both pre-sniffing and TreeBuilder.doctype parser enforcement.
    """
    if isinstance(content, str):
        text = content
        content_bytes = content.encode("utf-8")
    else:
        content_bytes = content
        text = None
        # Check 4-byte BOMs or headers first before 2-byte BOMs
        if content_bytes.startswith(b"\xff\xfe\x00\x00") or content_bytes.startswith(
            b"<\x00\x00\x00"
        ):
            text = content_bytes.decode("utf-32-le", errors="replace")
        elif content_bytes.startswith(b"\x00\x00\xfe\xff") or content_bytes.startswith(
            b"\x00\x00\x00<"
        ):
            text = content_bytes.decode("utf-32-be", errors="replace")
        elif content_bytes.startswith(b"\xfe\xff") or content_bytes.startswith(b"\x00<\x00?"):
            text = content_bytes.decode("utf-16-be", errors="replace")
        elif content_bytes.startswith(b"\xff\xfe") or content_bytes.startswith(b"<\x00?\x00"):
            text = content_bytes.decode("utf-16-le", errors="replace")
        elif b"\x00" in content_bytes[:4]:
            try:
                text = content_bytes.decode("utf-16", errors="replace")
            except Exception:
                pass

        if text is None:
            # Check declared encoding in XML declaration
            m = re.search(rb"""<\?xml[^>]+encoding=['"]([^'"]+)['"]""", content_bytes[:200])
            if m:
                enc = m.group(1).decode("ascii", errors="ignore").strip()
                try:
                    text = content_bytes.decode(enc, errors="replace")
                except Exception:
                    text = content_bytes.decode("utf-8", errors="replace")
            else:
                text = content_bytes.decode("utf-8", errors="replace")

    assert text is not None
    lower_text = text.lower()

    if "<!doctype" in lower_text:
        raise ValueError("Forbidden DOCTYPE declaration detected in XML (XXE defense)")

    if "<!entity" in lower_text:
        raise ValueError("Forbidden entity declaration detected in XML (XXE defense)")

    parser = ET.XMLParser(target=DTDRejectingTreeBuilder())
    parser.feed(content_bytes)
    return parser.close()


class ArtifactStore:
    """Content-addressed immutable store for raw artifacts."""

    def __init__(self, root: str | Path) -> None:
        self.root = Path(root).resolve()
        self.root.mkdir(parents=True, exist_ok=True)
        self.tmp_dir = self.root / "_tmp"
        self.tmp_dir.mkdir(parents=True, exist_ok=True)

    def _normalize_ext(self, ext: str) -> str:
        if not ext:
            return ""
        if any(c in ext for c in ("/\\:\0")) or ".." in ext:
            raise ValueError(f"Invalid extension contains forbidden characters: {ext!r}")
        normalized = ext if ext.startswith(".") else f".{ext}"
        if not _SAFE_EXT_RE.match(normalized):
            raise ValueError(f"Invalid extension format: {ext!r}")
        return normalized

    def path_for(self, sha256: str, *, ext: str = "") -> Path:
        """Return the content-addressed filesystem path for a given sha256 digest.

        Enforces that the computed path strictly resides within the store root.
        """
        clean_sha = sha256.lower().strip()
        if not _SHA256_RE.match(clean_sha):
            raise ValueError(f"Invalid SHA256 digest: {sha256!r}")
        normalized_ext = self._normalize_ext(ext)
        target = (self.root / clean_sha[:2] / f"{clean_sha}{normalized_ext}").resolve()
        try:
            target.relative_to(self.root)
        except ValueError:
            raise ValueError(f"Artifact path escapes store root directory: {target}")

        curr = target.parent
        while curr != self.root:
            if curr.exists() and curr.is_symlink():
                try:
                    curr.resolve().relative_to(self.root)
                except ValueError:
                    raise ValueError(f"Artifact parent symlink escapes store root: {curr}")
            if curr == curr.parent:
                break
            curr = curr.parent

        return target

    def has(self, sha256: str, *, ext: str = "") -> bool:
        """Check whether the artifact exists in the store."""
        return self.path_for(sha256, ext=ext).is_file()

    def store(self, content: bytes, *, ext: str = "") -> str:
        """Save bytes exclusively and atomically under its SHA256 hex digest.

        Verifies integrity if target already exists; avoids overwriting immutable artifacts.
        Uses atomic hardlink or exclusive open fallback to prevent race overwrites.
        Returns 64-character lowercase SHA256 hex digest.
        """
        sha256 = hashlib.sha256(content).hexdigest().lower()
        target_path = self.path_for(sha256, ext=ext)

        if target_path.is_file():
            existing = target_path.read_bytes()
            existing_sha = hashlib.sha256(existing).hexdigest().lower()
            if existing_sha != sha256 or existing != content:
                raise ValueError(
                    f"Integrity check failed: existing artifact {target_path} is corrupt"
                )
            return sha256

        target_path.parent.mkdir(parents=True, exist_ok=True)

        tmp_file = self.tmp_dir / f"{uuid.uuid4().hex}.tmp"
        try:
            tmp_file.write_bytes(content)
            try:
                os.link(tmp_file, target_path)
            except FileExistsError:
                # File created concurrently; verify existing integrity
                existing = target_path.read_bytes()
                existing_sha = hashlib.sha256(existing).hexdigest().lower()
                if existing_sha != sha256 or existing != content:
                    raise ValueError(
                        f"Integrity check failed: existing artifact {target_path} is corrupt"
                    )
            except OSError as exc:
                # Require hardlink success or fail closed; never overwrite
                raise RuntimeError(
                    f"Atomic publish failed: hardlink creation failed ({exc}). Failing closed."
                ) from exc
        finally:
            tmp_file.unlink(missing_ok=True)

        return sha256

    def get(self, sha256: str, *, ext: str = "") -> bytes:
        """Retrieve raw bytes for sha256.

        Raises FileNotFoundError if absent, ValueError on hash mismatch.
        """
        path = self.path_for(sha256, ext=ext)
        if not path.is_file():
            raise FileNotFoundError(f"Artifact not found in store: {sha256}")

        data = path.read_bytes()
        actual_sha = hashlib.sha256(data).hexdigest().lower()
        if actual_sha != sha256.lower().strip():
            raise ValueError(f"Integrity check failed: expected {sha256}, got {actual_sha}")
        return data

    def extract_zip_safe(
        self,
        zip_bytes: bytes,
        target_dir: str | Path,
        *,
        max_files: int = 1000,
        max_total_size: int = 50 * 1024 * 1024,
        max_single_size: int = 20 * 1024 * 1024,
    ) -> list[Path]:
        """Safely extract zip bytes preventing path traversal, overwrites, and zip bombs.

        Returns list of extracted paths relative to target_dir.
        """
        target_path = Path(target_dir).resolve()
        target_path.mkdir(parents=True, exist_ok=True)

        zf = zipfile.ZipFile(io.BytesIO(zip_bytes))
        infolist = zf.infolist()

        if len(infolist) > max_files:
            raise ValueError(f"Zip bomb: file count limit exceeded ({len(infolist)} > {max_files})")

        total_uncompressed = 0
        extracted_paths: list[Path] = []
        seen_names: set[str] = set()
        seen_casefold: set[str] = set()

        for info in infolist:
            norm_name = info.filename.replace("\\", "/")
            path_parts = Path(norm_name).parts

            if (
                any(part == ".." for part in path_parts)
                or norm_name.startswith("/")
                or ":" in norm_name
            ):
                raise ValueError(f"Path traversal detected in zip archive: {info.filename}")

            casefold_name = norm_name.lower()
            if norm_name in seen_names or casefold_name in seen_casefold:
                raise ValueError(f"Duplicate or casefold collision entry in zip: {info.filename}")
            seen_names.add(norm_name)
            seen_casefold.add(casefold_name)

            dest = (target_path / norm_name).resolve()
            try:
                dest.relative_to(target_path)
            except ValueError:
                raise ValueError(f"Unsafe destination path outside target: {info.filename}")

            # Symlink check (Unix mode 0o120000)
            attr_mode = (info.external_attr >> 16) & 0o170000
            if attr_mode == 0o120000:
                raise ValueError(f"Symlinks forbidden in archive: {info.filename}")

            if info.is_dir():
                dest.mkdir(parents=True, exist_ok=True)
                continue

            if dest.exists():
                raise ValueError(f"Zip extraction would overwrite existing file: {dest}")

            dest.parent.mkdir(parents=True, exist_ok=True)

            file_read = 0
            with zf.open(info) as src, open(dest, "xb") as out:
                while True:
                    chunk = src.read(65536)
                    if not chunk:
                        break
                    file_read += len(chunk)
                    total_uncompressed += len(chunk)
                    if file_read > max_single_size:
                        raise ValueError(
                            f"Single file size limit exceeded for {info.filename}: "
                            f"{file_read} > {max_single_size}"
                        )
                    if total_uncompressed > max_total_size:
                        raise ValueError(
                            f"Total uncompressed size limit exceeded: "
                            f"{total_uncompressed} > {max_total_size}"
                        )
                    out.write(chunk)

            extracted_paths.append(dest.relative_to(target_path))

        return extracted_paths
