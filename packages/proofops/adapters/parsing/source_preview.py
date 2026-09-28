"""Bounded, per-request PDF page preview rendering for private source reads."""

from __future__ import annotations

import json
import math
import subprocess
import sys
from dataclasses import asdict
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace

from proofops.adapters.parsing.opendataloader import OpenDataLoaderParser, ParseFailure
from proofops.domain.documents import PageGeometry

_MAX_SOURCE_BYTES = 100 * 1024 * 1024
_MAX_OUTPUT_BYTES = 16 * 1024 * 1024
_MAX_DIMENSION = 2000
_MAX_PIXELS = 4_000_000


class SourcePreviewFailure(ValueError):
    """Sanitized preview error; PDF internals never reach the client."""


def render_page_preview(
    source: bytes,
    physical_page: int,
    geometry: PageGeometry,
    *,
    include_annotations_and_forms: bool = False,
) -> tuple[bytes, float, float]:
    """Render one graph-selected page in an isolated PDFium process."""
    if not isinstance(source, bytes) or len(source) > _MAX_SOURCE_BYTES:
        raise SourcePreviewFailure("SOURCE_PREVIEW_INPUT_LIMIT")
    if type(physical_page) is not int or physical_page < 1:
        raise SourcePreviewFailure("SOURCE_PREVIEW_PAGE_INVALID")
    if type(include_annotations_and_forms) is not bool:
        raise SourcePreviewFailure("SOURCE_PREVIEW_MODE_INVALID")
    profile = SimpleNamespace(
        timeout_seconds=15,
        memory_bytes=512 * 1024 * 1024,
        max_output_bytes=_MAX_OUTPUT_BYTES,
    )
    with TemporaryDirectory(prefix=".source-preview-") as temporary:
        work = Path(temporary)
        (work / "source.pdf").write_bytes(source)
        (work / "request.json").write_text(
            json.dumps(
                {
                    "physical_page": physical_page,
                    "geometry": asdict(geometry),
                    "include_annotations_and_forms": include_annotations_and_forms,
                }
            )
        )
        try:
            OpenDataLoaderParser._execute(
                [sys.executable, "-I", str(Path(__file__).resolve()), str(work)],
                work,
                {"PATH": "/usr/bin:/bin", "PYTHONDONTWRITEBYTECODE": "1"},
                profile,
            )
            result = json.loads((work / "result.json").read_bytes())
            png = (work / "output.png").read_bytes()
        except (OSError, ValueError, KeyError, TypeError, subprocess.SubprocessError, ParseFailure):
            raise SourcePreviewFailure("SOURCE_PREVIEW_FAILED") from None
    if (
        not isinstance(result, dict)
        or set(result) != {"width_pt", "height_pt"}
        or any(
            isinstance(result[name], bool)
            or not isinstance(result[name], int | float)
            or not math.isfinite(result[name])
            or result[name] <= 0
            for name in result
        )
        or len(png) > _MAX_OUTPUT_BYTES
        or not png.startswith(b"\x89PNG\r\n\x1a\n")
    ):
        raise SourcePreviewFailure("SOURCE_PREVIEW_OUTPUT_INVALID")
    return png, float(result["width_pt"]), float(result["height_pt"])


def _child(work: Path) -> None:
    import resource

    import pypdfium2 as pdfium

    request = json.loads((work / "request.json").read_bytes())
    page_number = request["physical_page"]
    expected = PageGeometry(**request["geometry"])
    if type(page_number) is not int or page_number < 1:
        raise ValueError("invalid page")
    resource.setrlimit(resource.RLIMIT_CPU, (15, 15))
    resource.setrlimit(resource.RLIMIT_FSIZE, (_MAX_OUTPUT_BYTES, _MAX_OUTPUT_BYTES))
    resource.setrlimit(resource.RLIMIT_CORE, (0, 0))
    document = pdfium.PdfDocument(work / "source.pdf")
    page = None
    bitmap = None
    image = None
    try:
        include_appearance = request.get("include_annotations_and_forms", False)
        if type(include_appearance) is not bool:
            raise ValueError("invalid appearance mode")
        if include_appearance:
            if document.get_formtype() not in (
                pdfium.raw.FORMTYPE_NONE,
                pdfium.raw.FORMTYPE_ACRO_FORM,
            ):
                raise ValueError("unsupported XFA form appearance")
            document.init_forms()
        page = document.get_page(page_number - 1)  # fixed 0-based PDFium index
        width_pt, height_pt = page.get_size()
        crop_box = tuple(float(value) for value in page.get_bbox())
        rotation = int(page.get_rotation()) % 360
        actual = PageGeometry(width_pt, height_pt, rotation, crop_box)
        # PDFium converts PDF decimal coordinates to float32; allow subpixel rounding only.
        if actual.rotation != expected.rotation or not all(
            math.isclose(left, right, rel_tol=0, abs_tol=0.0001)
            for left, right in zip(
                (actual.width_pt, actual.height_pt, *actual.crop_box),
                (expected.width_pt, expected.height_pt, *expected.crop_box),
                strict=True,
            )
        ):
            raise ValueError("graph geometry mismatch")
        # Leave one pixel of headroom against renderer rounding at every ceiling.
        scale = min(
            2.0,
            1999.0 / width_pt,
            1999.0 / height_pt,
            math.sqrt(3_996_001 / (width_pt * height_pt)),
        )
        bitmap = page.render(
            scale=scale,
            may_draw_forms=include_appearance,
            draw_annots=include_appearance,
            limit_image_cache=True,
        )
        image = bitmap.to_pil()
        if image.width > _MAX_DIMENSION or image.height > _MAX_DIMENSION:
            raise ValueError("render dimensions")
        if image.width * image.height > _MAX_PIXELS:
            raise ValueError("render pixels")
        image.save(work / "output.png", format="PNG")
        (work / "result.json").write_text(
            json.dumps({"width_pt": width_pt, "height_pt": height_pt})
        )
    finally:
        if image is not None:
            image.close()
        if bitmap is not None:
            bitmap.close()
        if page is not None:
            page.close()
        document.close()


if __name__ == "__main__":
    _child(Path(sys.argv[1]))
