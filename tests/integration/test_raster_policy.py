"""Raster policy pins the actual local helpers and installed readers."""

from hashlib import sha256
from importlib.metadata import version
from pathlib import Path

import pytest
from proofops.adapters.local import raster_ocr, raster_visibility
from proofops.adapters.local.run_artifacts import native_paragraph_policy
from proofops.domain.provenance import canonical_hash


def test_raster_ocr_policy_pins_real_helpers_and_reader_versions():
    assert raster_visibility.raster_ocr_policy(mode="enhanced", max_pages=10, max_calls=20) == {
        "schema": "local_raster_ocr_policy_v1",
        "mode": "enhanced",
        "max_pages": 10,
        "max_calls": 20,
        "native_policy_sha256": canonical_hash(native_paragraph_policy()),
        "raster_helper_sha256": sha256(Path(raster_ocr.__file__).read_bytes()).hexdigest(),
        "composition_helper_sha256": sha256(
            Path(raster_visibility.__file__).read_bytes()
        ).hexdigest(),
        "checkpoint_helper_sha256": sha256(
            Path(raster_visibility.__file__).with_name("raster_checkpoint.py").read_bytes()
        ).hexdigest(),
        "reader_versions": {
            "pypdfium2": version("pypdfium2"),
            "pdfplumber": version("pdfplumber"),
            "pypdf": version("pypdf"),
            "Pillow": version("Pillow"),
        },
    }


@pytest.mark.parametrize(
    "kwargs",
    [
        {"mode": "other"},
        {"mode": True},
        {"max_pages": True},
        {"max_pages": 1.0},
        {"max_pages": 0},
        {"max_pages": 11},
        {"max_calls": False},
        {"max_calls": 1.0},
        {"max_calls": 0},
        {"max_calls": 21},
    ],
)
def test_raster_ocr_policy_rejects_invalid_limits(kwargs):
    with pytest.raises(ValueError):
        raster_visibility.raster_ocr_policy(**kwargs)


def test_evaluation_raster_exports_remain_compatible():
    from evaluation import native_raster_visibility
    from evaluation import raster_ocr as legacy_raster_ocr

    assert legacy_raster_ocr.prepare_raster_ocr is raster_ocr.prepare_raster_ocr
    assert native_raster_visibility.corroborate_native_visibility is (
        raster_visibility.corroborate_native_visibility
    )
