"""Immutable page/coordinate/native-text provenance values.

Pure module: stdlib only (dataclasses/typing/math), matching the domain
purity boundary enforced by `scripts/verify_architecture.py`. No AWS SDK, no
network, no file access, no environment variables.

docs/27_PARSING_AND_PROVENANCE.md §3 fixes three numbers that are never
conflated: `physical_page` (1-based, PDF file order -- the only one stored),
`page_index` (0-based, always derived as `physical_page - 1`), and
`printed_page_label` (whatever string is printed on the page, independent of
both). §3-4 also fixes the canonical bbox contract: rotation- and
CropBox-aware, top-left origin, PDF points, `[x0,y0,x1,y1]`; missing geometry
is `None`, never a zero bbox (FX-LOC-001).

`NativeSource` carries one parser candidate's native (pre-canonicalization)
geometry and raw text for a single source location, and `PageGeometry`
carries the per-page rotation/CropBox/size needed to canonicalize it. Both
are consumed by `application/ingest/geometry.canonicalize_source_ref`.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Final, Literal

from proofops.domain.errors import DomainValidationError
from proofops.domain.values import _require_uuid

Rotation = Literal[0, 90, 180, 270]

_ROTATIONS: Final = (0, 90, 180, 270)
# docs/27 §2-3: only the PDF native bottom-left-origin-points coordinate
# system is supported by `PageGeometry.to_canonical_affine`. Any other
# native_coordinate_system value must be rejected explicitly rather than
# silently having the bottom-left transform applied to coordinates it was
# never derived for.
SUPPORTED_NATIVE_COORDINATE_SYSTEMS: Final = frozenset({"pdf_bottom_left_points"})


def _require_finite_bbox(
    name: str, value: tuple[float, float, float, float] | None
) -> tuple[float, float, float, float] | None:
    if value is None:
        return None
    if not isinstance(value, tuple | list) or len(value) != 4:
        raise DomainValidationError(f"{name} must have exactly 4 coordinates")
    coords: list[float] = []
    for coord in value:
        if isinstance(coord, bool) or not isinstance(coord, int | float):
            raise DomainValidationError(f"{name} coordinates must be numbers")
        number = float(coord)
        if not math.isfinite(number):
            raise DomainValidationError(f"{name} coordinates must be finite")
        coords.append(number)
    x0, y0, x1, y1 = coords
    if not (x0 < x1 and y0 < y1):
        raise DomainValidationError(f"{name} must satisfy left < right and bottom < top")
    return (x0, y0, x1, y1)


@dataclass(frozen=True, slots=True)
class PageGeometry:
    """Per-page geometry needed to canonicalize native coordinates.

    `width_pt`/`height_pt` are the *displayed* (post-rotation) page extents.
    `crop_box` is in the page's native (pre-rotation, bottom-left origin)
    coordinate system, `[left, bottom, right, top]`, matching CropBox as
    stored in the PDF. `rotation` is the page's `/Rotate` value normalized to
    {0, 90, 180, 270}, clockwise.
    """

    width_pt: float
    height_pt: float
    rotation: Rotation
    crop_box: tuple[float, float, float, float]

    def __post_init__(self) -> None:
        for name, value in (("width_pt", self.width_pt), ("height_pt", self.height_pt)):
            if isinstance(value, bool) or not isinstance(value, int | float):
                raise DomainValidationError(f"{name} must be a number")
            if not math.isfinite(value) or value <= 0:
                raise DomainValidationError(f"{name} must be a finite positive number")
        if isinstance(self.rotation, bool) or self.rotation not in _ROTATIONS:
            raise DomainValidationError(f"rotation must be one of {_ROTATIONS} (bool rejected)")
        crop = _require_finite_bbox("crop_box", self.crop_box)
        assert crop is not None  # crop_box is required, not optional
        object.__setattr__(self, "crop_box", crop)

        # The displayed width/height must be consistent with the CropBox
        # extents once rotation is applied: 0/180 keep width<->crop_width and
        # height<->crop_height, 90/270 swap them. A mismatch here means the
        # caller supplied width_pt/height_pt that do not actually describe
        # the rotated crop, which would silently corrupt every canonical
        # bbox derived from it.
        crop_left, crop_bottom, crop_right, crop_top = crop
        crop_width = crop_right - crop_left
        crop_height = crop_top - crop_bottom
        if self.rotation in (0, 180):
            expected_width, expected_height = crop_width, crop_height
        else:
            expected_width, expected_height = crop_height, crop_width
        tolerance = 1e-6
        if (
            abs(self.width_pt - expected_width) > tolerance
            or abs(self.height_pt - expected_height) > tolerance
        ):
            raise DomainValidationError(
                "width_pt/height_pt are inconsistent with crop_box under the "
                f"given rotation: expected ({expected_width}, {expected_height}), "
                f"got ({self.width_pt}, {self.height_pt})"
            )

    def to_canonical_affine(self) -> tuple[float, float, float, float, float, float]:
        """Return the native->canonical affine `(a,b,c,d,e,f)`.

        Applied as `x' = a*x + c*y + e`, `y' = b*x + d*y + f` (PDF/SVG
        convention). The transform: (1) translates the CropBox origin to
        (0,0), (2) rotates clockwise by `rotation` about that origin, (3)
        flips the bottom-left PDF y-axis to the top-left display y-axis.

        docs/27 §3: for rotation=0 and crop origin=(0,0) this reduces to the
        documented `[left, H-top, right, H-bottom]` formula.
        """
        crop_left, crop_bottom, crop_right, crop_top = self.crop_box
        crop_width = crop_right - crop_left
        crop_height = crop_top - crop_bottom

        # Step 1: translate so the crop origin is (0, 0), in native
        # bottom-left-origin space.
        translate = (1.0, 0.0, 0.0, 1.0, -crop_left, -crop_bottom)

        # Step 2: rotate clockwise about the origin within the crop's own
        # bottom-left frame. After translation the crop occupies
        # [0, crop_width] x [0, crop_height].
        if self.rotation == 0:
            rotate = (1.0, 0.0, 0.0, 1.0, 0.0, 0.0)
        elif self.rotation == 90:
            rotate = (0.0, -1.0, 1.0, 0.0, 0.0, crop_width)
        elif self.rotation == 180:
            rotate = (-1.0, 0.0, 0.0, -1.0, crop_width, crop_height)
        else:  # 270
            rotate = (0.0, 1.0, -1.0, 0.0, crop_height, 0.0)

        combined = _compose_affine(rotate, translate)

        # Step 3: flip bottom-left origin (native PDF space, y up) to
        # top-left origin (display space, y down) using the *displayed*
        # page height after rotation.
        flip = (1.0, 0.0, 0.0, -1.0, 0.0, self.height_pt)
        return _compose_affine(flip, combined)


def _compose_affine(
    outer: tuple[float, float, float, float, float, float],
    inner: tuple[float, float, float, float, float, float],
) -> tuple[float, float, float, float, float, float]:
    """Return `outer . inner`: apply `inner` first, then `outer`."""
    a1, b1, c1, d1, e1, f1 = outer
    a2, b2, c2, d2, e2, f2 = inner
    return (
        a1 * a2 + c1 * b2,
        b1 * a2 + d1 * b2,
        a1 * c2 + c1 * d2,
        b1 * c2 + d1 * d2,
        a1 * e2 + c1 * f2 + e1,
        b1 * e2 + d1 * f2 + f1,
    )


@dataclass(frozen=True, slots=True)
class NativeSource:
    """One parser candidate's native geometry + raw text for a location.

    `native_bbox` is `None` when the parser could not locate the text at all
    (FX-LOC-001) -- it is never a zero bbox. `char_start`/`char_end` are
    Unicode code point offsets into `raw_text`, end-exclusive.
    """

    document_version_id: str
    parse_manifest_id: str
    parser_run_id: str
    source_native_id: str
    physical_page: int
    printed_page_label: str | None
    native_bbox: tuple[float, float, float, float] | None
    native_coordinate_system: str
    raw_text: str
    char_start: int
    char_end: int

    def __post_init__(self) -> None:
        for name in ("document_version_id", "parse_manifest_id", "parser_run_id"):
            _require_uuid(name, getattr(self, name))
        if not isinstance(self.source_native_id, str) or not self.source_native_id:
            raise DomainValidationError("source_native_id must be a non-empty string")
        if isinstance(self.physical_page, bool) or not isinstance(self.physical_page, int):
            raise DomainValidationError("physical_page must be an int")
        if self.physical_page < 1:
            raise DomainValidationError("physical_page must be >= 1 (1-based)")
        if self.printed_page_label is not None and not isinstance(self.printed_page_label, str):
            raise DomainValidationError("printed_page_label must be a string or null")
        bbox = _require_finite_bbox("native_bbox", self.native_bbox)
        object.__setattr__(self, "native_bbox", bbox)
        # docs/27 §2-3: OpenDataLoader's [left,bottom,right,top] must not be
        # mixed with other native coordinate conventions. Only the one
        # system this module's affine math was derived for is accepted;
        # anything else is rejected rather than silently transformed as if
        # it were bottom-left-origin PDF points.
        if self.native_coordinate_system not in SUPPORTED_NATIVE_COORDINATE_SYSTEMS:
            raise DomainValidationError(
                "native_coordinate_system must be one of "
                f"{sorted(SUPPORTED_NATIVE_COORDINATE_SYSTEMS)}, "
                f"got {self.native_coordinate_system!r}"
            )
        if not isinstance(self.raw_text, str):
            raise DomainValidationError("raw_text must be a string")
        for name in ("char_start", "char_end"):
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, int):
                raise DomainValidationError(f"{name} must be an int")
        if self.char_start < 0 or self.char_end < self.char_start:
            raise DomainValidationError("char offsets must satisfy 0 <= start <= end")
        if self.char_end > len(self.raw_text):
            raise DomainValidationError("char_end must not exceed len(raw_text)")

    @property
    def page_index(self) -> int:
        """0-based renderer index, always derived from physical_page."""
        return self.physical_page - 1
