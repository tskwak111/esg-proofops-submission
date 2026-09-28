"""AT-002 / FR-002 acceptance tests: page/coordinate/native-text round trip.

Fixed contract under test: `canonicalize_source_ref :: NativeSource +
PageGeometry -> SourceRef` (docs/20_TASK_BREAKDOWN.md TASK-002,
docs/27_PARSING_AND_PROVENANCE.md §3-4).

Every case must assert an actual round trip: canonical bbox derived from a
native bbox + page geometry (rotation, CropBox origin shift) must invert back
to the native bbox within <0.5pt (FX-LOC-002), and normalized-text character
offsets must map back to the exact raw substring for NFD-decomposed Korean
input (FX-LOC-003). Missing geometry must stay `None`/`unlocated`, never a
zero bbox (FX-LOC-001).
"""

from __future__ import annotations

import unicodedata

import pytest
from proofops.application.ingest.geometry import (
    affine_apply,
    canonicalize_source_ref,
    invert_affine,
)
from proofops.domain.documents import NativeSource, PageGeometry
from proofops.domain.errors import DomainValidationError
from proofops.domain.values import SourceRef

DOC_VERSION_ID = "00000000-0000-4000-8000-000000000002"
PARSE_MANIFEST_ID = "00000000-0000-4000-8000-000000000003"
SOURCE_ID = "00000000-0000-4000-8000-000000000001"


def _sha256_of(text: str) -> str:
    import hashlib

    return hashlib.sha256(text.encode("utf-8")).hexdigest()


class TestUnrotatedCropAtOrigin:
    """Baseline: rotation=0, crop origin=(0,0) -- docs/27 §3 simple formula."""

    def test_canonical_matches_simple_formula(self) -> None:
        # Page is 612x792 pt (US Letter), no rotation, crop box == media box.
        geometry = PageGeometry(
            width_pt=612.0,
            height_pt=792.0,
            rotation=0,
            crop_box=(0.0, 0.0, 612.0, 792.0),
        )
        # Native bbox is PDF bottom-left origin [left, bottom, right, top].
        native_bbox = (100.0, 700.0, 200.0, 730.0)
        raw_text = "2030년까지"
        source = NativeSource(
            document_version_id=DOC_VERSION_ID,
            parse_manifest_id=PARSE_MANIFEST_ID,
            parser_run_id="00000000-0000-4000-8000-000000000008",
            source_native_id="synthetic-block-1",
            physical_page=28,
            printed_page_label="26",
            native_bbox=native_bbox,
            native_coordinate_system="pdf_bottom_left_points",
            raw_text=raw_text,
            char_start=0,
            char_end=len(raw_text),
        )
        ref = canonicalize_source_ref(source, geometry, source_id=SOURCE_ID)

        assert isinstance(ref, SourceRef)
        left, bottom, right, top = native_bbox
        expected = (left, geometry.height_pt - top, right, geometry.height_pt - bottom)
        assert ref.bbox is not None
        for got, want in zip(ref.bbox, expected, strict=True):
            assert abs(got - want) < 1e-9
        assert ref.page_num == 28
        assert ref.printed_page_label == "26"
        assert ref.location_quality == "located"
        assert ref.quote == raw_text


class TestRotationAndCropBoxRoundTrip:
    """FX-LOC-002: rotation 90/180/270 + CropBox origin shift round trip <0.5pt.

    Expected canonical boxes below were computed by an independent reference
    implementation (not the module under test): translate native bbox into
    crop-local bottom-left coordinates, rotate the crop rectangle clockwise
    by `rotation` about its own origin (swapping width/height for 90/270),
    then flip to a top-left origin using the *displayed* page height. This
    guards against the round-trip-only assertion (invert-then-compare with
    the same matrix) trivially passing even if the forward transform itself
    were wrong (e.g. a consistently mirrored or off-by-quadrant rotation).
    """

    # crop_box = (20, 15, 575, 800) -> crop_width=555, crop_height=785.
    CROP_BOX = (20.0, 15.0, 575.0, 800.0)
    NATIVE_BBOX = (120.0, 470.0, 180.0, 502.0)
    # (rotation, width_pt, height_pt, expected_canonical_bbox)
    CASES = [
        (0, 555.0, 785.0, (100.0, 298.0, 160.0, 330.0)),
        (90, 785.0, 555.0, (455.0, 100.0, 487.0, 160.0)),
        (180, 555.0, 785.0, (395.0, 455.0, 455.0, 487.0)),
        (270, 785.0, 555.0, (298.0, 395.0, 330.0, 455.0)),
    ]

    @pytest.mark.parametrize("rotation,width_pt,height_pt,expected", CASES)
    def test_canonical_bbox_matches_independent_reference(
        self,
        rotation: int,
        width_pt: float,
        height_pt: float,
        expected: tuple[float, float, float, float],
    ) -> None:
        geometry = PageGeometry(
            width_pt=width_pt,
            height_pt=height_pt,
            rotation=rotation,  # type: ignore[arg-type]
            crop_box=self.CROP_BOX,
        )
        source = NativeSource(
            document_version_id=DOC_VERSION_ID,
            parse_manifest_id=PARSE_MANIFEST_ID,
            parser_run_id="00000000-0000-4000-8000-000000000008",
            source_native_id="synthetic-block-2",
            physical_page=5,
            printed_page_label=None,
            native_bbox=self.NATIVE_BBOX,
            native_coordinate_system="pdf_bottom_left_points",
            raw_text="회전 테스트",
            char_start=0,
            char_end=len("회전 테스트"),
        )
        ref = canonicalize_source_ref(source, geometry, source_id=SOURCE_ID)
        assert ref.bbox is not None
        for got, want in zip(ref.bbox, expected, strict=True):
            assert abs(got - want) < 1e-6, f"rotation={rotation}: {ref.bbox} != {expected}"

    @pytest.mark.parametrize("rotation,width_pt,height_pt,_expected", CASES)
    def test_round_trip_within_half_point(
        self,
        rotation: int,
        width_pt: float,
        height_pt: float,
        _expected: tuple[float, float, float, float],
    ) -> None:
        geometry = PageGeometry(
            width_pt=width_pt,
            height_pt=height_pt,
            rotation=rotation,  # type: ignore[arg-type]
            crop_box=self.CROP_BOX,
        )
        source = NativeSource(
            document_version_id=DOC_VERSION_ID,
            parse_manifest_id=PARSE_MANIFEST_ID,
            parser_run_id="00000000-0000-4000-8000-000000000008",
            source_native_id="synthetic-block-2",
            physical_page=5,
            printed_page_label=None,
            native_bbox=self.NATIVE_BBOX,
            native_coordinate_system="pdf_bottom_left_points",
            raw_text="회전 테스트",
            char_start=0,
            char_end=len("회전 테스트"),
        )
        ref = canonicalize_source_ref(source, geometry, source_id=SOURCE_ID)
        assert ref.bbox is not None

        # Round trip: invert the same affine and recover the native corners.
        # (This alone cannot prove the forward transform is correct -- see
        # test_canonical_bbox_matches_independent_reference above for that --
        # but it does catch inverse-transform bugs independently.)
        matrix = geometry.to_canonical_affine()
        inverse = invert_affine(matrix)
        cx0, cy0, cx1, cy1 = ref.bbox
        recovered_corners = [
            affine_apply(inverse, cx0, cy0),
            affine_apply(inverse, cx1, cy0),
            affine_apply(inverse, cx1, cy1),
            affine_apply(inverse, cx0, cy1),
        ]
        rx = [pt[0] for pt in recovered_corners]
        ry = [pt[1] for pt in recovered_corners]
        recovered_bbox = (min(rx), min(ry), max(rx), max(ry))

        for got, want in zip(recovered_bbox, self.NATIVE_BBOX, strict=True):
            assert abs(got - want) < 0.5, f"round trip error too large: {got} vs {want}"

        # Canonical bbox must stay within the crop's display extents.
        x0, y0, crop_x1, crop_y1 = ref.bbox
        assert 0.0 <= x0 <= geometry.width_pt
        assert 0.0 <= y0 <= geometry.height_pt
        assert 0.0 <= crop_x1 <= geometry.width_pt
        assert 0.0 <= crop_y1 <= geometry.height_pt


class TestPageGeometryValidation:
    """PageGeometry must reject internally-inconsistent or malformed input."""

    def test_rejects_width_height_inconsistent_with_rotated_crop(self) -> None:
        # crop is 555x785 (native); rotation=90 must swap to 785x555, not
        # keep 555x785 -- a caller passing the unrotated size is a bug that
        # would silently corrupt every canonical bbox on this page.
        with pytest.raises(DomainValidationError):
            PageGeometry(
                width_pt=555.0,
                height_pt=785.0,
                rotation=90,
                crop_box=(20.0, 15.0, 575.0, 800.0),
            )

    def test_rejects_bool_rotation(self) -> None:
        with pytest.raises(DomainValidationError):
            PageGeometry(
                width_pt=555.0,
                height_pt=785.0,
                rotation=True,  # type: ignore[arg-type]
                crop_box=(20.0, 15.0, 575.0, 800.0),
            )

    def test_rejects_unsupported_native_coordinate_system(self) -> None:
        geometry = PageGeometry(
            width_pt=555.0, height_pt=785.0, rotation=0, crop_box=(20.0, 15.0, 575.0, 800.0)
        )
        with pytest.raises(DomainValidationError):
            NativeSource(
                document_version_id=DOC_VERSION_ID,
                parse_manifest_id=PARSE_MANIFEST_ID,
                parser_run_id="00000000-0000-4000-8000-000000000008",
                source_native_id="synthetic-block-unsupported-coords",
                physical_page=1,
                printed_page_label=None,
                native_bbox=(100.0, 100.0, 200.0, 200.0),
                native_coordinate_system="opendataloader_left_bottom_right_top",
                raw_text="x",
                char_start=0,
                char_end=1,
            )
        assert geometry.rotation == 0  # geometry itself remains valid/unused

    def test_rejects_out_of_bounds_canonical_bbox(self) -> None:
        # native_bbox extends past the crop's own extents (crop is
        # 555x785, this bbox reaches x=600 which is outside crop_right-20).
        geometry = PageGeometry(
            width_pt=555.0, height_pt=785.0, rotation=0, crop_box=(20.0, 15.0, 575.0, 800.0)
        )
        source = NativeSource(
            document_version_id=DOC_VERSION_ID,
            parse_manifest_id=PARSE_MANIFEST_ID,
            parser_run_id="00000000-0000-4000-8000-000000000008",
            source_native_id="synthetic-block-oob",
            physical_page=1,
            printed_page_label=None,
            native_bbox=(590.0, 100.0, 620.0, 130.0),  # entirely right of crop_right=575
            native_coordinate_system="pdf_bottom_left_points",
            raw_text="x",
            char_start=0,
            char_end=1,
        )
        with pytest.raises(DomainValidationError):
            canonicalize_source_ref(source, geometry, source_id=SOURCE_ID)


class TestKoreanCombiningCharacterOffsets:
    """FX-LOC-003: NFD-decomposed Korean text must not be confused with raw offsets."""

    def test_nfc_normalization_map_round_trips_to_raw_offsets(self) -> None:
        # "탄소중립" written as NFD (each syllable decomposed into jamo).
        raw_text_nfd = unicodedata.normalize("NFD", "탄소중립 목표")
        geometry = PageGeometry(
            width_pt=595.0, height_pt=842.0, rotation=0, crop_box=(0.0, 0.0, 595.0, 842.0)
        )
        native_bbox = (50.0, 700.0, 250.0, 720.0)
        source = NativeSource(
            document_version_id=DOC_VERSION_ID,
            parse_manifest_id=PARSE_MANIFEST_ID,
            parser_run_id="00000000-0000-4000-8000-000000000008",
            source_native_id="synthetic-block-3",
            physical_page=12,
            printed_page_label="12",
            native_bbox=native_bbox,
            native_coordinate_system="pdf_bottom_left_points",
            raw_text=raw_text_nfd,
            char_start=0,
            char_end=len(raw_text_nfd),
        )
        ref = canonicalize_source_ref(source, geometry, source_id=SOURCE_ID)

        # The composed (NFC) quote must match what a UI would search for,
        # but the underlying raw text hash must still be over the raw (NFD)
        # bytes -- normalization must never silently replace provenance.
        normalized = unicodedata.normalize("NFC", raw_text_nfd)
        assert ref.quote == raw_text_nfd or ref.quote == normalized
        assert ref.raw_text_sha256 == _sha256_of(raw_text_nfd)

    def test_normalized_offset_is_not_mistaken_for_raw_offset(self) -> None:
        # A composed-form substring offset must map back to the correct raw
        # (possibly longer, decomposed) substring rather than an
        # index-shifted, wrong slice of the raw text.
        raw_text_nfd = unicodedata.normalize("NFD", "탄소중립 목표 연도")
        normalized = unicodedata.normalize("NFC", raw_text_nfd)
        # Locate "목표" in the *normalized* string (as an LLM/UI would).
        norm_start = normalized.index("목표")
        norm_end = norm_start + len("목표")

        geometry = PageGeometry(
            width_pt=595.0, height_pt=842.0, rotation=0, crop_box=(0.0, 0.0, 595.0, 842.0)
        )
        source = NativeSource(
            document_version_id=DOC_VERSION_ID,
            parse_manifest_id=PARSE_MANIFEST_ID,
            parser_run_id="00000000-0000-4000-8000-000000000008",
            source_native_id="synthetic-block-4",
            physical_page=12,
            printed_page_label="12",
            native_bbox=(50.0, 700.0, 250.0, 720.0),
            native_coordinate_system="pdf_bottom_left_points",
            raw_text=raw_text_nfd,
            char_start=0,
            char_end=len(raw_text_nfd),
        )
        ref = canonicalize_source_ref(
            source,
            geometry,
            source_id=SOURCE_ID,
            normalized_char_start=norm_start,
            normalized_char_end=norm_end,
        )

        raw_slice = raw_text_nfd[ref.char_start : ref.char_end]
        assert unicodedata.normalize("NFC", raw_slice) == "목표"
        # Using the normalized offsets directly on raw text would be wrong
        # whenever decomposition changed the length upstream of the match.
        if len(raw_text_nfd) != len(normalized):
            assert (ref.char_start, ref.char_end) != (norm_start, norm_end)

    def test_latin_combining_marks_round_trip(self) -> None:
        # "café" decomposed: e + COMBINING ACUTE ACCENT (U+0301), embedded in
        # a longer string, mirroring reordered-combining-mark ambiguity.
        raw_text_nfd = unicodedata.normalize("NFD", "The café report")
        normalized = unicodedata.normalize("NFC", raw_text_nfd)
        norm_start = normalized.index("café")
        norm_end = norm_start + len("café")

        geometry = PageGeometry(
            width_pt=595.0, height_pt=842.0, rotation=0, crop_box=(0.0, 0.0, 595.0, 842.0)
        )
        source = NativeSource(
            document_version_id=DOC_VERSION_ID,
            parse_manifest_id=PARSE_MANIFEST_ID,
            parser_run_id="00000000-0000-4000-8000-000000000008",
            source_native_id="synthetic-block-4b",
            physical_page=12,
            printed_page_label="12",
            native_bbox=(50.0, 700.0, 250.0, 720.0),
            native_coordinate_system="pdf_bottom_left_points",
            raw_text=raw_text_nfd,
            char_start=0,
            char_end=len(raw_text_nfd),
        )
        ref = canonicalize_source_ref(
            source,
            geometry,
            source_id=SOURCE_ID,
            normalized_char_start=norm_start,
            normalized_char_end=norm_end,
        )
        raw_slice = raw_text_nfd[ref.char_start : ref.char_end]
        assert unicodedata.normalize("NFC", raw_slice) == "café"


class TestMissingGeometryStaysUnlocated:
    """FX-LOC-001: bbox must be None (never zero-bbox) when geometry is missing."""

    def test_no_native_bbox_produces_none_bbox_and_unlocated(self) -> None:
        geometry = PageGeometry(
            width_pt=595.0, height_pt=842.0, rotation=0, crop_box=(0.0, 0.0, 595.0, 842.0)
        )
        source = NativeSource(
            document_version_id=DOC_VERSION_ID,
            parse_manifest_id=PARSE_MANIFEST_ID,
            parser_run_id="00000000-0000-4000-8000-000000000008",
            source_native_id="synthetic-block-5",
            physical_page=1,
            printed_page_label=None,
            native_bbox=None,
            native_coordinate_system="pdf_bottom_left_points",
            raw_text="근거 없음",
            char_start=0,
            char_end=len("근거 없음"),
        )
        ref = canonicalize_source_ref(source, geometry, source_id=SOURCE_ID)
        assert ref.bbox is None
        assert ref.location_quality == "unlocated"


class TestPhysicalVsPrintedVsPageIndex:
    """docs/27 §3: physical_page, page_index, printed_page_label are never conflated."""

    def test_physical_page_is_required_and_printed_label_is_independent(self) -> None:
        geometry = PageGeometry(
            width_pt=595.0, height_pt=842.0, rotation=0, crop_box=(0.0, 0.0, 595.0, 842.0)
        )
        source = NativeSource(
            document_version_id=DOC_VERSION_ID,
            parse_manifest_id=PARSE_MANIFEST_ID,
            parser_run_id="00000000-0000-4000-8000-000000000008",
            source_native_id="synthetic-block-6",
            physical_page=34,
            printed_page_label="GRI-12",  # printed label is a free string
            native_bbox=(10.0, 10.0, 20.0, 20.0),
            native_coordinate_system="pdf_bottom_left_points",
            raw_text="x",
            char_start=0,
            char_end=1,
        )
        ref = canonicalize_source_ref(source, geometry, source_id=SOURCE_ID)
        assert ref.page_num == 34
        assert ref.printed_page_label == "GRI-12"

    def test_physical_page_below_one_is_rejected(self) -> None:
        with pytest.raises(DomainValidationError):
            NativeSource(
                document_version_id=DOC_VERSION_ID,
                parse_manifest_id=PARSE_MANIFEST_ID,
                parser_run_id="00000000-0000-4000-8000-000000000008",
                source_native_id="synthetic-block-7",
                physical_page=0,
                printed_page_label=None,
                native_bbox=None,
                native_coordinate_system="pdf_bottom_left_points",
                raw_text="x",
                char_start=0,
                char_end=1,
            )


class TestOutOfBoundsAndInvertedCoordinatesRejected:
    """docs/27 §3: out-of-crop / NaN / inverted native coords are a parse issue, not silent."""

    def test_inverted_native_bbox_is_rejected(self) -> None:
        with pytest.raises(DomainValidationError):
            NativeSource(
                document_version_id=DOC_VERSION_ID,
                parse_manifest_id=PARSE_MANIFEST_ID,
                parser_run_id="00000000-0000-4000-8000-000000000008",
                source_native_id="synthetic-block-8",
                physical_page=1,
                printed_page_label=None,
                native_bbox=(200.0, 700.0, 100.0, 730.0),  # right < left
                native_coordinate_system="pdf_bottom_left_points",
                raw_text="x",
                char_start=0,
                char_end=1,
            )

    def test_non_finite_native_bbox_is_rejected(self) -> None:
        with pytest.raises(DomainValidationError):
            NativeSource(
                document_version_id=DOC_VERSION_ID,
                parse_manifest_id=PARSE_MANIFEST_ID,
                parser_run_id="00000000-0000-4000-8000-000000000008",
                source_native_id="synthetic-block-9",
                physical_page=1,
                printed_page_label=None,
                native_bbox=(float("nan"), 0.0, 10.0, 10.0),
                native_coordinate_system="pdf_bottom_left_points",
                raw_text="x",
                char_start=0,
                char_end=1,
            )
