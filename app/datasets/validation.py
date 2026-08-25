"""Dataset quality control: validation, duplicate detection, quality score.

Ported from Apollo `app/services/dataset_validation.py` — the accidental
duplicate function definitions at the bottom of the original were removed
during the port; behavior is unchanged.
"""

from __future__ import annotations

import hashlib
import logging
from dataclasses import dataclass, field
from typing import Any

logger = logging.getLogger(__name__)

MIN_DIMENSION = 256
MAX_DIMENSION = 8192

NEAR_HASH_EXACT = 0
NEAR_HASH_CLOSE = 6

ASPECT_RATIO_TOLERANCE = 0.05
FLAT_COLOR_DISTANCE = 12
LOW_ENTROPY_BITS = 4

WEIGHTS = {
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


@dataclass
class ImageFinding:
    image_id: str
    filename: str
    kind: str
    detail: str = ""

    def to_dict(self) -> dict[str, str]:
        return {
            "image_id": self.image_id,
            "filename": self.filename,
            "kind": self.kind,
            "detail": self.detail,
        }


@dataclass
class DatasetReport:
    total_images: int = 0
    valid_images: int = 0
    captioned: int = 0
    findings: list[ImageFinding] = field(default_factory=list)
    average_resolution: tuple[int, int] | None = None
    score: int = 100

    @property
    def issue_count(self) -> int:
        return len(self.findings)

    def to_dict(self) -> dict[str, Any]:
        avg = self.average_resolution
        return {
            "total_images": self.total_images,
            "valid_images": self.valid_images,
            "invalid_images": self.total_images - self.valid_images,
            "captioned": self.captioned,
            "missing_captions": sum(1 for f in self.findings if f.kind == "missing_caption"),
            "duplicates": sum(1 for f in self.findings if f.kind == "duplicate"),
            "near_duplicates": sum(1 for f in self.findings if f.kind == "near_duplicate"),
            "possible_duplicates": sum(
                1 for f in self.findings if f.kind == "near_duplicate_possible"
            ),
            "extreme_resolutions": sum(1 for f in self.findings if f.kind == "extreme_resolution"),
            "average_resolution": list(avg) if avg else None,
            "issue_count": self.issue_count,
            "score": self.score,
            "findings": [f.to_dict() for f in self.findings],
        }


def _perceptual_hash(image: Any) -> str:
    """8x8 average-hash (aHash) as a 64-bit hex string."""
    gray = image.convert("L").resize((8, 8))
    pixels = list(gray.getdata())
    mean = sum(pixels) / len(pixels)
    bits = 0
    for pixel in pixels:
        bits = (bits << 1) | (1 if pixel > mean else 0)
    return f"{bits:016x}"


def _hamming(a: str, b: str) -> int:
    return bin(int(a, 16) ^ int(b, 16)).count("1")


def _pixel_sha256(image: Any) -> str:
    return hashlib.sha256(image.convert("RGB").tobytes()).hexdigest()


def _mean_color(image: Any) -> tuple[float, float, float]:
    small = image.convert("RGB").resize((1, 1))
    return small.getpixel((0, 0))


def _color_distance(a: tuple[float, float, float], b: tuple[float, float, float]) -> float:
    return max(abs(x - y) for x, y in zip(a, b))


def _is_low_entropy(hash_hex: str) -> bool:
    bits = int(hash_hex, 16)
    ones = bin(bits).count("1")
    return ones <= LOW_ENTROPY_BITS or ones >= 64 - LOW_ENTROPY_BITS


def _same_aspect_ratio(w_a: int, h_a: int, w_b: int, h_b: int) -> bool:
    ratio_a = w_a / max(h_a, 1)
    ratio_b = w_b / max(h_b, 1)
    if ratio_a == 0 or ratio_b == 0:
        return False
    return abs(ratio_a - ratio_b) / max(ratio_a, ratio_b) <= ASPECT_RATIO_TOLERANCE


def classify_pair(
    hash_a: str,
    hash_b: str,
    *,
    dims_a: tuple[int, int] | None = None,
    dims_b: tuple[int, int] | None = None,
    color_a: tuple[float, float, float] | None = None,
    color_b: tuple[float, float, float] | None = None,
) -> str:
    """Classify similarity using every available signal.

    Returns ``near_duplicate_high``, ``near_duplicate_possible`` or ``unique``.
    Pixel-identical copies are caught earlier by SHA-256.
    """
    distance = _hamming(hash_a, hash_b)

    if distance == 0:
        if _is_low_entropy(hash_a) and color_a is not None and color_b is not None:
            if _color_distance(color_a, color_b) > FLAT_COLOR_DISTANCE:
                return "unique"
        return "near_duplicate_high"

    if distance > NEAR_HASH_CLOSE:
        return "unique"

    corroboration = 0
    required = 0

    if dims_a and dims_b:
        required += 1
        if _same_aspect_ratio(*dims_a, *dims_b):
            corroboration += 1

    if _is_low_entropy(hash_a) or _is_low_entropy(hash_b):
        if color_a is not None and color_b is not None:
            required += 1
            if _color_distance(color_a, color_b) <= FLAT_COLOR_DISTANCE:
                corroboration += 1

    if required and corroboration < required:
        return "near_duplicate_possible"
    return "near_duplicate_high"


def validate_dataset(
    images: list[dict[str, Any]],
    read_bytes,
) -> DatasetReport:
    """Validate a dataset of ``{id, filename, caption}`` dicts.

    ``read_bytes(image_id) -> bytes`` is injectable so tests can feed
    synthetic images without anything on disk.
    """
    from io import BytesIO

    from PIL import Image

    report = DatasetReport(total_images=len(images))
    widths: list[int] = []
    heights: list[int] = []
    exact: dict[str, str] = {}
    fingerprints: list[dict[str, Any]] = []

    for entry in images:
        image_id = entry["id"]
        filename = entry.get("filename", image_id)
        caption = (entry.get("caption") or "").strip()

        if not caption:
            report.findings.append(ImageFinding(image_id, filename, "missing_caption"))
        elif len(caption) < 3:
            report.findings.append(ImageFinding(image_id, filename, "empty_caption", caption))
        elif _looks_suspicious(caption):
            report.findings.append(
                ImageFinding(image_id, filename, "suspicious_caption", caption[:80])
            )
        else:
            report.captioned += 1

        try:
            content = read_bytes(image_id)
            with Image.open(BytesIO(content)) as image:
                image.load()
                width, height = image.size
                pixel_hash = _pixel_sha256(image)
                phash = _perceptual_hash(image)
                color = _mean_color(image)
        except Exception as exc:  # noqa: BLE001
            logger.info("[DATASET] %s failed to decode: %s", filename, exc)
            report.findings.append(ImageFinding(image_id, filename, "invalid", str(exc)[:120]))
            continue

        report.valid_images += 1
        widths.append(width)
        heights.append(height)

        if width < MIN_DIMENSION or height < MIN_DIMENSION:
            report.findings.append(ImageFinding(
                image_id, filename, "extreme_resolution",
                f"{width}x{height} below {MIN_DIMENSION}px minimum"))
        elif width > MAX_DIMENSION or height > MAX_DIMENSION:
            report.findings.append(ImageFinding(
                image_id, filename, "extreme_resolution",
                f"{width}x{height} above {MAX_DIMENSION}px maximum"))

        if pixel_hash in exact:
            report.findings.append(ImageFinding(
                image_id, filename, "duplicate", f"identical to {exact[pixel_hash]}"))
        else:
            exact[pixel_hash] = image_id

        fingerprints.append({
            "id": image_id,
            "hash": phash,
            "dims": (width, height),
            "color": color,
        })

    seen_pairs: set[tuple[str, str]] = set()
    for i, fp_a in enumerate(fingerprints):
        for fp_b in fingerprints[i + 1:]:
            verdict = classify_pair(
                fp_a["hash"], fp_b["hash"],
                dims_a=fp_a["dims"], dims_b=fp_b["dims"],
                color_a=fp_a["color"], color_b=fp_b["color"],
            )
            if verdict == "unique":
                continue

            pair = tuple(sorted((fp_a["id"], fp_b["id"])))
            if pair in seen_pairs:
                continue
            seen_pairs.add(pair)
            later_id = pair[1]
            kind = ("near_duplicate" if verdict == "near_duplicate_high"
                    else "near_duplicate_possible")
            report.findings.append(ImageFinding(
                later_id, _filename_of(images, later_id), kind,
                f"{verdict.replace('_', ' ')} vs {pair[0]}"))

    if widths and len(set(zip(widths, heights))) > 1:
        distinct = len(set(zip(widths, heights)))
        report.findings.append(ImageFinding(
            "*", "(dataset)", "dimension_inconsistency",
            f"{distinct} distinct dimensions; AI Toolkit will bucket them"))

    if widths:
        report.average_resolution = (
            round(sum(widths) / len(widths)),
            round(sum(heights) / len(heights)),
        )

    report.score = _score(report)
    return report


def _filename_of(images: list[dict[str, Any]], image_id: str) -> str:
    for entry in images:
        if entry["id"] == image_id:
            return entry.get("filename", image_id)
    return image_id


def _looks_suspicious(caption: str) -> bool:
    lowered = caption.lower()
    markers = (
        "as an ai", "i cannot", "i can't", "sorry",
        "lorem ipsum", "undefined", "null,", "[error",
    )
    return any(marker in lowered for marker in markers)


def _score(report: DatasetReport) -> int:
    counts: dict[str, int] = {}
    for finding in report.findings:
        counts[finding.kind] = counts.get(finding.kind, 0) + 1

    penalty = 0
    for kind, count in counts.items():
        weight = WEIGHTS.get(kind, 2)
        penalty += min(count, 10) * weight

    return max(0, 100 - penalty)
