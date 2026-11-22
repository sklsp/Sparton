"""Unit tests for app/datasets/validation.py (dataset quality control).

The pure logic (caption checks, near-duplicate classification, scoring) is tested
directly. ``validate_dataset`` orchestration and the pixel helpers are exercised
with a fake PIL module injected into ``sys.modules``, so every executable line of
the module runs even in environments without Pillow installed. Tests that decode
real image bytes sit behind ``pytest.importorskip("PIL")`` (Pillow is an
experimental extra, not in requirements.txt).
"""

from __future__ import annotations

import hashlib
import json
import sys
import types
from typing import Any

import pytest

from app.datasets.validation import (
    ASPECT_RATIO_TOLERANCE,
    FLAT_COLOR_DISTANCE,
    LOW_ENTROPY_BITS,
    MAX_DIMENSION,
    MIN_DIMENSION,
    NEAR_HASH_CLOSE,
    NEAR_HASH_EXACT,
    WEIGHTS,
    DatasetReport,
    ImageFinding,
    _color_distance,
    _filename_of,
    _hamming,
    _is_low_entropy,
    _looks_suspicious,
    _mean_color,
    _perceptual_hash,
    _pixel_sha256,
    _same_aspect_ratio,
    _score,
    classify_pair,
    validate_dataset,
)

# Four 64-bit hashes pairwise more than NEAR_HASH_CLOSE apart: images with these
# perceptual hashes are never flagged as near duplicates of each other.
WELL_SEPARATED = [0x0, 0xF0F0F0F0F0F0F0F0, 0x0F0F0F0F0F0F0F0F, 0x7F7F7F7F7F7F7F7F]

# A normal-entropy hash (32 set bits) used for near-duplicate pair tests.
NORMAL_HASH = 0xAAAAAAAAAAAAAAA


# --------------------------------------------------------------------------
# Fake PIL — duck-typed stand-ins so validate_dataset's `from PIL import Image`
# resolves without Pillow installed.
# --------------------------------------------------------------------------


class _FakeGrayImage:
    """8x8 grayscale image whose pixels reproduce a target bit pattern exactly.

    Pixels are 1/2, so ``pixel > mean`` is True exactly for the set bits of the
    target (for any popcount <= 63; an all-ones target degenerates to zero).
    """

    def __init__(self, bits: int) -> None:
        self._bits = bits

    def resize(self, size):
        assert size == (8, 8)
        return self

    def getdata(self):
        return [2 if (self._bits >> (63 - i)) & 1 else 1 for i in range(64)]


class _FakeRGBImage:
    """RGB image with deterministic bytes and a fixed mean color."""

    def __init__(self, seed: str, color: tuple[int, int, int]) -> None:
        self._seed = seed
        self._color = color

    def tobytes(self):
        return hashlib.sha256(f"rgb:{self._seed}".encode()).digest() * 4

    def resize(self, size):
        assert size == (1, 1)
        return self

    def getpixel(self, pos):
        assert pos == (0, 0)
        return self._color


class _FakeImage:
    """Duck-typed PIL.Image.Image built from a JSON spec."""

    def __init__(self, spec: dict[str, Any]) -> None:
        self._spec = spec
        self._size = tuple(spec["size"])

    @property
    def size(self):
        return self._size

    def load(self) -> None:
        pass

    def convert(self, mode: str):
        if mode == "L":
            return _FakeGrayImage(int(self._spec.get("phash", 0)))
        return _FakeRGBImage(str(self._spec["seed"]), tuple(self._spec["color"]))

    def close(self) -> None:
        pass

    def __enter__(self):
        return self

    def __exit__(self, *exc_info):
        self.close()
        return False


def _install_fake_pil(monkeypatch: pytest.MonkeyPatch) -> None:
    """Point ``from PIL import Image`` at a fake module for the duration of a test."""

    class _ImageModule:
        @staticmethod
        def open(stream):
            spec = json.loads(stream.read())
            if spec.get("corrupt"):
                raise ValueError(spec.get("error", "truncated data"))
            return _FakeImage(spec)

    module = types.ModuleType("PIL")
    module.Image = _ImageModule  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "PIL", module)


def _image_bytes(
    size=(320, 320),
    seed="a",
    phash=0,
    color=(128, 64, 32),
    corrupt=False,
    error=None,
) -> bytes:
    spec: dict[str, Any] = {
        "size": list(size),
        "seed": seed,
        "phash": phash,
        "color": list(color),
    }
    if corrupt:
        spec["corrupt"] = True
        if error is not None:
            spec["error"] = error
    return json.dumps(spec).encode("utf-8")


def _run(entries: list[dict[str, Any]], bytes_map: dict[str, bytes], monkeypatch) -> DatasetReport:
    """Run validate_dataset with the fake PIL and a read_bytes backed by bytes_map."""
    _install_fake_pil(monkeypatch)
    return validate_dataset(entries, lambda image_id: bytes_map[image_id])


# --------------------------------------------------------------------------
# Constants — the documented thresholds of the ported module.
# --------------------------------------------------------------------------


class TestConstants:
    def test_resolution_bounds(self):
        assert MIN_DIMENSION == 256
        assert MAX_DIMENSION == 8192

    def test_hash_bands(self):
        assert NEAR_HASH_EXACT == 0
        assert NEAR_HASH_CLOSE == 6

    def test_corroboration_thresholds(self):
        assert ASPECT_RATIO_TOLERANCE == 0.05
        assert FLAT_COLOR_DISTANCE == 12
        assert LOW_ENTROPY_BITS == 4

    def test_score_weights(self):
        assert WEIGHTS == {
            "invalid": 15,
            "duplicate": 8,
            "near_duplicate": 5,
            "near_duplicate_possible": 2,
            "extreme_resolution": 6,
            "missing_caption": 5,
            "empty_caption": 5,
            "suspicious_caption": 3,
            "dimension_inconsistency": 2,
        }


# --------------------------------------------------------------------------
# Dataclasses.
# --------------------------------------------------------------------------


class TestImageFinding:
    def test_to_dict_roundtrip(self):
        finding = ImageFinding("7", "img.png", "duplicate", "identical to 3")
        assert finding.to_dict() == {
            "image_id": "7",
            "filename": "img.png",
            "kind": "duplicate",
            "detail": "identical to 3",
        }

    def test_default_detail_is_empty(self):
        assert ImageFinding("1", "a.png", "missing_caption").detail == ""


class TestDatasetReport:
    def test_defaults(self):
        report = DatasetReport()
        assert (report.total_images, report.valid_images, report.captioned) == (0, 0, 0)
        assert report.findings == []
        assert report.average_resolution is None
        assert report.score == 100
        assert report.issue_count == 0

    def test_issue_count_counts_findings(self):
        report = DatasetReport(findings=[ImageFinding("a", "a.png", "duplicate")] * 3)
        assert report.issue_count == 3

    def test_to_dict_with_mixed_findings(self):
        findings = [
            ImageFinding("1", "a.png", "missing_caption"),
            ImageFinding("2", "b.png", "missing_caption"),
            ImageFinding("3", "c.png", "duplicate", "identical to 1"),
            ImageFinding("4", "d.png", "near_duplicate", "near duplicate high vs 1"),
            ImageFinding("5", "e.png", "near_duplicate_possible", "near duplicate possible vs 2"),
            ImageFinding("6", "f.png", "extreme_resolution", "below 256px minimum"),
        ]
        report = DatasetReport(
            total_images=7, valid_images=6, captioned=1, findings=findings, average_resolution=(320, 240)
        )
        assert report.to_dict() == {
            "total_images": 7,
            "valid_images": 6,
            "invalid_images": 1,
            "captioned": 1,
            "missing_captions": 2,
            "duplicates": 1,
            "near_duplicates": 1,
            "possible_duplicates": 1,
            "extreme_resolutions": 1,
            "average_resolution": [320, 240],
            "issue_count": 6,
            "score": 100,
            "findings": [f.to_dict() for f in findings],
        }

    def test_to_dict_average_resolution_none(self):
        assert DatasetReport().to_dict()["average_resolution"] is None


# --------------------------------------------------------------------------
# Pure helpers.
# --------------------------------------------------------------------------


class TestHamming:
    def test_identical_hashes_differ_by_zero(self):
        assert _hamming("ab" * 8, "ab" * 8) == 0

    def test_single_bit_flip_differs_by_one(self):
        assert _hamming(format(NORMAL_HASH, "016x"), format(NORMAL_HASH ^ (1 << 3), "016x")) == 1

    def test_complement_hashes_differ_by_sixty_four(self):
        assert _hamming("f" * 16, "0" * 16) == 64


class TestLowEntropy:
    def test_zero_hash_is_low_entropy(self):
        assert _is_low_entropy("0" * 16) is True

    def test_all_ones_hash_is_low_entropy(self):
        assert _is_low_entropy("f" * 16) is True

    def test_four_set_bits_is_the_inclusive_boundary(self):
        assert _is_low_entropy(format(0xF, "016x")) is True

    def test_five_set_bits_is_not_low_entropy(self):
        assert _is_low_entropy(format(0x1F, "016x")) is False

    def test_sixty_set_bits_is_the_inclusive_boundary(self):
        assert _is_low_entropy(format((1 << 60) - 1, "016x")) is True

    def test_fifty_nine_set_bits_is_not_low_entropy(self):
        assert _is_low_entropy(format((1 << 59) - 1, "016x")) is False


class TestSameAspectRatio:
    def test_same_ratio_matches(self):
        assert _same_aspect_ratio(320, 240, 640, 480) is True

    def test_swapped_dimensions_do_not_match(self):
        assert _same_aspect_ratio(320, 240, 240, 320) is False

    def test_relative_tolerance_boundary_is_inclusive(self):
        # |19 - 20| / max == exactly ASPECT_RATIO_TOLERANCE (0.05).
        assert _same_aspect_ratio(19, 1, 20, 1) is True

    def test_beyond_relative_tolerance_does_not_match(self):
        # |19 - 20.5| / 20.5 ~= 0.073 > tolerance.
        assert _same_aspect_ratio(19, 1, 41, 2) is False

    def test_zero_width_is_not_a_match(self):
        assert _same_aspect_ratio(0, 100, 0, 200) is False


class TestColorDistance:
    def test_identical_colors_differ_by_zero(self):
        assert _color_distance((10, 50, 90), (10, 50, 90)) == 0

    def test_max_channel_difference_wins(self):
        assert _color_distance((10, 50, 90), (20, 48, 80)) == 10

    def test_flat_color_boundary_value(self):
        assert _color_distance((0, 0, 0), (FLAT_COLOR_DISTANCE, 0, 0)) == FLAT_COLOR_DISTANCE


class TestLooksSuspicious:
    @pytest.mark.parametrize(
        "caption",
        [
            "As an AI assistant I cannot describe this image.",
            "I can't see that image for you.",
            "Sorry, no image was provided.",
            "lorem ipsum dolor sit amet",
            "the value is undefined here",
            "null, then the caption crashes",
            "[error] decode failed at byte 0",
        ],
    )
    def test_markers_are_flagged_case_insensitively(self, caption):
        assert _looks_suspicious(caption) is True

    def test_clean_caption_is_not_flagged(self):
        assert _looks_suspicious("red car, parked on street, soft light") is False


class TestClassifyPair:
    def test_far_hash_is_unique(self):
        assert classify_pair(format(NORMAL_HASH, "016x"), format(WELL_SEPARATED[1], "016x")) == "unique"

    def test_close_hash_without_signals_is_high_confidence(self):
        # No dims and no colors: nothing to corroborate against, so a close hash stands.
        near = format(NORMAL_HASH ^ (1 << 3), "016x")
        assert classify_pair(format(NORMAL_HASH, "016x"), near) == "near_duplicate_high"

    def test_close_hash_with_matching_aspect_is_high_confidence(self):
        assert (
            classify_pair(
                format(NORMAL_HASH, "016x"),
                format(NORMAL_HASH ^ (1 << 5), "016x"),
                dims_a=(320, 320),
                dims_b=(640, 640),
            )
            == "near_duplicate_high"
        )

    def test_close_hash_with_different_aspect_is_possible(self):
        assert (
            classify_pair(
                format(NORMAL_HASH, "016x"),
                format(NORMAL_HASH ^ (1 << 5), "016x"),
                dims_a=(320, 320),
                dims_b=(640, 320),
            )
            == "near_duplicate_possible"
        )

    def test_flat_identical_hash_with_far_colors_is_unique(self):
        # Identical low-entropy hashes (flat images) with very different mean colors:
        # two different flat fills, not a duplicate.
        assert (
            classify_pair(
                "0" * 16,
                "0" * 16,
                color_a=(255, 0, 0),
                color_b=(0, 0, 255),
            )
            == "unique"
        )

    def test_flat_close_hash_with_far_colors_downgrades_to_possible(self):
        # Close hashes on flat images: the far mean colors fail corroboration.
        assert (
            classify_pair(
                "0" * 16,
                format(1 << 3, "016x"),
                color_a=(255, 0, 0),
                color_b=(0, 0, 255),
            )
            == "near_duplicate_possible"
        )

    def test_flat_images_with_close_colors_are_high_confidence(self):
        assert (
            classify_pair(
                "0" * 16,
                format(1 << 3, "016x"),
                color_a=(100, 100, 100),
                color_b=(105, 98, 102),
            )
            == "near_duplicate_high"
        )

    def test_flat_images_without_color_info_assume_match(self):
        assert classify_pair("0" * 16, format(1 << 3, "016x")) == "near_duplicate_high"

    def test_distance_six_is_still_a_candidate(self):
        flipped = NORMAL_HASH ^ ((1 << 3) | (1 << 5) | (1 << 7) | (1 << 9) | (1 << 11) | (1 << 13))
        assert _hamming(format(NORMAL_HASH, "016x"), format(flipped, "016x")) == NEAR_HASH_CLOSE
        assert classify_pair(format(NORMAL_HASH, "016x"), format(flipped, "016x")) == "near_duplicate_high"

    def test_distance_seven_is_unique(self):
        flipped = NORMAL_HASH ^ (
            (1 << 3) | (1 << 5) | (1 << 7) | (1 << 9) | (1 << 11) | (1 << 13) | (1 << 15)
        )
        assert classify_pair(format(NORMAL_HASH, "016x"), format(flipped, "016x")) == "unique"


class TestScore:
    def test_empty_report_scores_100(self):
        assert _score(DatasetReport()) == 100

    def test_single_finding_subtracts_its_weight(self):
        report = DatasetReport(findings=[ImageFinding("a", "a.png", "missing_caption")])
        assert _score(report) == 95

    def test_per_class_penalty_capped_at_ten_findings(self):
        findings = [ImageFinding(str(i), f"{i}.png", "missing_caption") for i in range(12)]
        # min(12, 10) * 5 = 50.
        assert _score(DatasetReport(findings=findings)) == 50

    def test_score_never_drops_below_zero(self):
        findings = [ImageFinding(str(i), f"{i}.png", kind) for i in range(10) for kind in WEIGHTS]
        assert _score(DatasetReport(findings=findings)) == 0

    def test_unknown_kind_uses_default_weight_two(self):
        findings = [ImageFinding(str(i), f"{i}.png", "mystery") for i in range(3)]
        assert _score(DatasetReport(findings=findings)) == 94


class TestFilenameOf:
    def test_returns_filename_when_present(self):
        entries = [{"id": "1", "filename": "a.png"}]
        assert _filename_of(entries, "1") == "a.png"

    def test_falls_back_to_image_id_when_key_missing(self):
        entries = [{"id": "1"}]
        assert _filename_of(entries, "1") == "1"

    def test_falls_back_to_image_id_when_entry_unknown(self):
        entries = [{"id": "2", "filename": "b.png"}]
        assert _filename_of(entries, "9") == "9"


# --------------------------------------------------------------------------
# Pixel helpers via duck-typed images (no Pillow required).
# --------------------------------------------------------------------------


class TestPixelHelpersWithFakeImages:
    def test_perceptual_hash_reproduces_bit_pattern(self):
        for target in (0x0, 1, 0x8000000000000000, NORMAL_HASH, WELL_SEPARATED[1]):
            image = _FakeImage({"size": [320, 320], "seed": "s", "phash": target, "color": [1, 2, 3]})
            assert _perceptual_hash(image) == f"{target:016x}"

    def test_perceptual_hash_all_ones_degenerates_to_zero(self):
        # Every pixel equals the mean, so no bit is set.
        image = _FakeImage({"size": [320, 320], "seed": "s", "phash": (1 << 64) - 1, "color": [1, 2, 3]})
        assert _perceptual_hash(image) == "0" * 16

    def test_pixel_sha256_is_deterministic_and_distinct(self):
        spec_a = {"size": [320, 320], "seed": "x", "phash": 0, "color": [1, 2, 3]}
        assert _pixel_sha256(_FakeImage(spec_a)) == _pixel_sha256(_FakeImage(dict(spec_a)))
        spec_b = dict(spec_a, seed="y")
        assert _pixel_sha256(_FakeImage(spec_a)) != _pixel_sha256(_FakeImage(spec_b))
        assert len(_pixel_sha256(_FakeImage(spec_a))) == 64

    def test_mean_color_roundtrips(self):
        image = _FakeImage({"size": [320, 320], "seed": "s", "phash": 0, "color": [10, 20, 30]})
        assert _mean_color(image) == (10, 20, 30)


# --------------------------------------------------------------------------
# validate_dataset orchestration with the fake PIL.
# --------------------------------------------------------------------------


class TestValidateDataset:
    def test_empty_dataset_scores_100(self, monkeypatch):
        report = _run([], {}, monkeypatch)
        assert report.total_images == 0
        assert report.findings == []
        assert report.average_resolution is None
        assert report.score == 100

    def test_missing_caption_findings(self, monkeypatch):
        entries: list[dict[str, Any]] = [
            {"id": "a", "filename": "a.png", "caption": ""},
            {"id": "b", "filename": "b.png"},  # no caption key at all
            {"id": "c", "filename": "c.png", "caption": None},
            {"id": "d", "filename": "d.png", "caption": "   \n\t "},
        ]
        data = {e["id"]: _image_bytes(seed=e["id"], phash=WELL_SEPARATED[i]) for i, e in enumerate(entries)}
        report = _run(entries, data, monkeypatch)
        missing = [f for f in report.findings if f.kind == "missing_caption"]
        assert len(missing) == 4
        assert {f.image_id for f in missing} == {"a", "b", "c", "d"}
        assert report.captioned == 0

    def test_caption_shorter_than_three_chars_is_empty(self, monkeypatch):
        entries = [
            {"id": "a", "filename": "a.png", "caption": "ab"},  # too short -> empty_caption
            {"id": "b", "filename": "b.png", "caption": "abc"},  # exactly three chars -> fine
        ]
        data = {e["id"]: _image_bytes(seed=e["id"], phash=WELL_SEPARATED[i]) for i, e in enumerate(entries)}
        report = _run(entries, data, monkeypatch)
        empty = [f for f in report.findings if f.kind == "empty_caption"]
        assert len(empty) == 1
        assert empty[0].image_id == "a"
        assert empty[0].detail == "ab"
        assert report.captioned == 1

    @pytest.mark.parametrize(
        "caption",
        [
            "As an AI I cannot describe this image.",
            "Sorry, that is undefined.",
            "[error] no image found",
        ],
    )
    def test_suspicious_caption_markers(self, monkeypatch, caption):
        entries = [{"id": "a", "filename": "a.png", "caption": caption}]
        report = _run(entries, {"a": _image_bytes(seed="a")}, monkeypatch)
        suspicious = [f for f in report.findings if f.kind == "suspicious_caption"]
        assert len(suspicious) == 1
        assert suspicious[0].detail == caption

    def test_suspicious_detail_truncated_to_80_chars(self, monkeypatch):
        caption = "x" * 120 + " sorry"
        entries = [{"id": "a", "filename": "a.png", "caption": caption}]
        report = _run(entries, {"a": _image_bytes(seed="a")}, monkeypatch)
        suspicious = [f for f in report.findings if f.kind == "suspicious_caption"]
        assert len(suspicious) == 1
        assert suspicious[0].detail == caption[:80]
        assert len(suspicious[0].detail) == 80

    def test_clean_dataset_scores_100(self, monkeypatch):
        entries = [
            {"id": "a", "filename": "a.png", "caption": "red car"},
            {"id": "b", "filename": "b.png", "caption": "blue sky"},
        ]
        data = {e["id"]: _image_bytes(seed=e["id"], phash=WELL_SEPARATED[i]) for i, e in enumerate(entries)}
        report = _run(entries, data, monkeypatch)
        assert report.total_images == 2
        assert report.valid_images == 2
        assert report.captioned == 2
        assert report.findings == []
        assert report.score == 100
        assert report.average_resolution == (320, 320)

    def test_undecodable_image_is_invalid(self, monkeypatch):
        entries = [
            {"id": "a", "filename": "a.png", "caption": "ok"},
            {"id": "b", "filename": "b.png", "caption": "broken"},
        ]
        data = {
            "a": _image_bytes(seed="a"),
            "b": _image_bytes(corrupt=True, error="truncated data"),
        }
        report = _run(entries, data, monkeypatch)
        invalid = [f for f in report.findings if f.kind == "invalid"]
        assert len(invalid) == 1
        assert invalid[0].image_id == "b"
        assert invalid[0].detail == "truncated data"
        assert report.valid_images == 1

    def test_below_minimum_resolution_flagged(self, monkeypatch):
        entries = [{"id": "a", "filename": "a.png", "caption": "tiny"}]
        report = _run(entries, {"a": _image_bytes(size=(100, 320))}, monkeypatch)
        extreme = [f for f in report.findings if f.kind == "extreme_resolution"]
        assert len(extreme) == 1
        assert extreme[0].detail == f"100x320 below {MIN_DIMENSION}px minimum"

    def test_above_maximum_resolution_flagged(self, monkeypatch):
        entries = [{"id": "a", "filename": "a.png", "caption": "huge"}]
        report = _run(entries, {"a": _image_bytes(size=(9000, 320))}, monkeypatch)
        extreme = [f for f in report.findings if f.kind == "extreme_resolution"]
        assert len(extreme) == 1
        assert extreme[0].detail == f"9000x320 above {MAX_DIMENSION}px maximum"

    def test_boundary_dimensions_not_flagged(self, monkeypatch):
        entries = [{"id": "a", "filename": "a.png", "caption": "edge"}]
        report = _run(entries, {"a": _image_bytes(size=(MIN_DIMENSION, MAX_DIMENSION))}, monkeypatch)
        assert [f for f in report.findings if f.kind == "extreme_resolution"] == []

    def test_exact_duplicate_flagged_on_later_image(self, monkeypatch):
        entries = [
            {"id": "a", "filename": "a.png", "caption": "one"},
            {"id": "b", "filename": "b.png", "caption": "two"},
        ]
        # Identical pixel bytes (same seed), far-apart perceptual hashes so only the
        # exact-duplicate signal fires.
        data = {
            "a": _image_bytes(seed="same", phash=WELL_SEPARATED[0]),
            "b": _image_bytes(seed="same", phash=WELL_SEPARATED[1]),
        }
        report = _run(entries, data, monkeypatch)
        dups = [f for f in report.findings if f.kind == "duplicate"]
        assert len(dups) == 1
        assert dups[0].image_id == "b"
        assert dups[0].detail == "identical to a"

    def test_near_duplicate_high(self, monkeypatch):
        entries = [
            {"id": "a", "filename": "a.png", "caption": "one"},
            {"id": "b", "filename": "b.png", "caption": "two"},
        ]
        data = {
            "a": _image_bytes(seed="a", phash=NORMAL_HASH),
            "b": _image_bytes(seed="b", phash=NORMAL_HASH ^ (1 << 3)),
        }
        report = _run(entries, data, monkeypatch)
        near = [f for f in report.findings if f.kind == "near_duplicate"]
        assert len(near) == 1
        assert near[0].image_id == "b"
        assert near[0].detail == "near duplicate high vs a"

    def test_near_duplicate_possible_when_structure_differs(self, monkeypatch):
        entries = [
            {"id": "a", "filename": "a.png", "caption": "one"},
            {"id": "b", "filename": "b.png", "caption": "two"},
        ]
        data = {
            "a": _image_bytes(seed="a", phash=NORMAL_HASH, size=(320, 320)),
            "b": _image_bytes(seed="b", phash=NORMAL_HASH ^ (1 << 5), size=(640, 320)),
        }
        report = _run(entries, data, monkeypatch)
        near = [f for f in report.findings if f.kind == "near_duplicate_possible"]
        assert len(near) == 1
        assert near[0].image_id == "b"
        assert near[0].detail == "near duplicate possible vs a"

    def test_dimension_inconsistency_is_informational(self, monkeypatch):
        entries = [
            {"id": "a", "filename": "a.png", "caption": "one"},
            {"id": "b", "filename": "b.png", "caption": "two"},
        ]
        data = {
            "a": _image_bytes(seed="a", phash=WELL_SEPARATED[0], size=(320, 320)),
            "b": _image_bytes(seed="b", phash=WELL_SEPARATED[1], size=(480, 480)),
        }
        report = _run(entries, data, monkeypatch)
        dim = [f for f in report.findings if f.kind == "dimension_inconsistency"]
        assert len(dim) == 1
        assert dim[0].image_id == "*"
        assert dim[0].filename == "(dataset)"
        assert "2 distinct dimensions" in dim[0].detail

    def test_average_resolution_rounded(self, monkeypatch):
        entries = [
            {"id": "a", "filename": "a.png", "caption": "one"},
            {"id": "b", "filename": "b.png", "caption": "two"},
        ]
        data = {
            "a": _image_bytes(seed="a", phash=WELL_SEPARATED[0], size=(300, 400)),
            "b": _image_bytes(seed="b", phash=WELL_SEPARATED[1], size=(302, 402)),
        }
        report = _run(entries, data, monkeypatch)
        assert report.average_resolution == (301, 401)

    def test_score_combines_findings(self, monkeypatch):
        # One missing caption (-5) and one exact duplicate (-8) -> 87.
        entries = [
            {"id": "a", "filename": "a.png", "caption": ""},
            {"id": "b", "filename": "b.png", "caption": "two"},
        ]
        data = {
            "a": _image_bytes(seed="same", phash=WELL_SEPARATED[0]),
            "b": _image_bytes(seed="same", phash=WELL_SEPARATED[1]),
        }
        report = _run(entries, data, monkeypatch)
        assert report.score == 87
        assert report.to_dict()["score"] == 87

    def test_finding_uses_image_id_when_filename_missing(self, monkeypatch):
        entries = [{"id": "a", "caption": ""}]
        report = _run(entries, {"a": _image_bytes(seed="a")}, monkeypatch)
        missing = [f for f in report.findings if f.kind == "missing_caption"]
        assert len(missing) == 1
        assert missing[0].filename == "a"

    def test_repeated_image_ids_do_not_double_flag_pairs(self, monkeypatch):
        # Degenerate input: two entries share id "p". The (p, q) pair is visited
        # twice by the double loop but must be flagged only once.
        entries = [
            {"id": "p", "filename": "p.png", "caption": "1"},
            {"id": "q", "filename": "q.png", "caption": "2"},
            {"id": "p", "filename": "p2.png", "caption": "3"},
        ]
        data = {
            "p": _image_bytes(seed="p", phash=NORMAL_HASH),
            "q": _image_bytes(seed="q", phash=NORMAL_HASH ^ 1),
        }
        report = _run(entries, data, monkeypatch)
        near = [f for f in report.findings if f.kind == "near_duplicate"]
        assert len(near) == 2


# --------------------------------------------------------------------------
# Real pixel decoding — skipped when Pillow is not installed.
# --------------------------------------------------------------------------


class TestRealPillowImages:
    @staticmethod
    def _png(color=(200, 30, 60), size=(320, 320)) -> bytes:
        from io import BytesIO

        from PIL import Image

        buffer = BytesIO()
        Image.new("RGB", size, color).save(buffer, format="PNG")
        return buffer.getvalue()

    def test_real_png_decodes_and_scores_100(self):
        pytest.importorskip("PIL")
        entries = [{"id": "a", "filename": "a.png", "caption": "red square"}]
        report = validate_dataset(entries, lambda image_id: self._png())
        assert report.valid_images == 1
        assert report.findings == []
        assert report.score == 100
        assert report.average_resolution == (320, 320)

    def test_identical_bytes_are_exact_duplicates(self):
        pytest.importorskip("PIL")
        data = self._png()
        entries = [
            {"id": "a", "filename": "a.png", "caption": "one"},
            {"id": "b", "filename": "b.png", "caption": "two"},
        ]
        report = validate_dataset(entries, lambda image_id: data)
        dup = next(f for f in report.findings if f.kind == "duplicate")
        assert dup.image_id == "b"
        assert dup.detail == "identical to a"

    def test_flat_different_colors_are_not_near_duplicates(self):
        pytest.importorskip("PIL")
        red, blue = self._png((255, 0, 0)), self._png((0, 0, 255))
        entries = [
            {"id": "a", "filename": "a.png", "caption": "red"},
            {"id": "b", "filename": "b.png", "caption": "blue"},
        ]
        report = validate_dataset(entries, lambda image_id: {"a": red, "b": blue}[image_id])
        assert report.findings == []
        assert report.score == 100

    def test_corrupt_bytes_marked_invalid(self):
        pytest.importorskip("PIL")
        entries = [{"id": "a", "filename": "a.png", "caption": "x"}]
        report = validate_dataset(entries, lambda image_id: b"this is not a png")
        invalid = [f for f in report.findings if f.kind == "invalid"]
        assert len(invalid) == 1
        assert report.valid_images == 0
