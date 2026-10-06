"""Measure Stage 1's thresholds against an external photo-and-caption dataset (#27).

    python calibration/measure_stage1.py --data <dir with pl.parquet> --out results.json

The dataset is the Polish split of Crossmodal-3600 (`floschne/xm3600` on
Hugging Face, CC BY 4.0): 3,600 photos, each described independently by one or
two people in Polish. It is **not** campaign data. It answers "how often would
honest, independent submissions trip a filter" and, with copies made here,
"how many simple re-uploads and caption edits does a threshold catch". It
cannot answer anything about real farming behaviour or submission timing.

Nothing here changes a threshold. It prints and saves measurements; the
decisions and their reasons are in calibration/README.md.
"""

from __future__ import annotations

import argparse
import io
import json
import random
import sys
import unicodedata
from collections import Counter
from datetime import datetime, timedelta, timezone
from pathlib import Path

import numpy as np
import pyarrow.parquet as pq
from PIL import Image, ImageEnhance, ImageOps

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from s2e_validator.prefilters import (  # noqa: E402
    FilterMode,
    Interaction,
    NewImage,
    PipelineConfig,
    build_pipeline,
    dhash,
    simhash,
)

SEED = 27
MAX_THRESHOLD = 20


def popcount(values: np.ndarray) -> np.ndarray:
    """Set bits of each uint64."""
    counts = np.zeros(values.shape, dtype=np.uint8)
    work = values.copy()
    for _ in range(64):
        counts += (work & np.uint64(1)).astype(np.uint8)
        work >>= np.uint64(1)
    return counts


def all_pair_distances(hashes: list[int]) -> tuple[np.ndarray, np.ndarray]:
    """(histogram of distances over all unordered pairs, nearest distance per item)."""
    array = np.array(hashes, dtype=np.uint64)
    histogram = np.zeros(65, dtype=np.int64)
    nearest = np.full(len(array), 64, dtype=np.uint8)
    for i in range(len(array) - 1):
        distances = popcount(array[i + 1:] ^ array[i])
        histogram += np.bincount(distances, minlength=65)
        nearest[i] = min(nearest[i], distances.min())
        np.minimum(nearest[i + 1:], distances, out=nearest[i + 1:])
    return histogram, nearest


def wide_pair_histogram(hashes: list[int], bits: int) -> list[int]:
    """Distances over all unordered pairs, for hashes wider than 64 bits."""
    histogram = [0] * (bits + 1)
    for i, left in enumerate(hashes):
        for right in hashes[i + 1:]:
            histogram[(left ^ right).bit_count()] += 1
    return histogram


def at_or_below(histogram: np.ndarray) -> dict[str, int]:
    cumulative = np.cumsum(histogram)
    return {str(t): int(cumulative[t]) for t in range(MAX_THRESHOLD + 1)}


def share_at_or_below(distances: list[int]) -> dict[str, float]:
    array = np.array(distances)
    return {str(t): round(float((array <= t).mean()), 4) for t in range(MAX_THRESHOLD + 1)}


def summary(distances: list[int]) -> dict[str, float]:
    array = np.array(distances)
    return {
        "n": int(array.size),
        "min": int(array.min()),
        "p05": float(np.percentile(array, 5)),
        "median": float(np.median(array)),
        "p95": float(np.percentile(array, 95)),
        "max": int(array.max()),
    }


# -- copies a farmer could make without any skill ------------------------------


def _save(image: Image.Image, fmt: str = "JPEG", **options) -> bytes:
    buffer = io.BytesIO()
    image.convert("RGB").save(buffer, fmt, **options)
    return buffer.getvalue()


def image_edits(data: bytes) -> dict[str, bytes]:
    image = Image.open(io.BytesIO(data))
    image.load()
    width, height = image.size

    def crop(share: float) -> Image.Image:
        dx, dy = int(width * share), int(height * share)
        return image.crop((dx, dy, width - dx, height - dy))

    return {
        "recompress_q60": _save(image, quality=60),
        "recompress_q30": _save(image, quality=30),
        "resize_half": _save(image.resize((max(1, width // 2), max(1, height // 2))), quality=85),
        "to_png": _save(image, "PNG"),
        "brighten_15pct": _save(ImageEnhance.Brightness(image).enhance(1.15), quality=85),
        "crop_3pct": _save(crop(0.03), quality=85),
        "crop_10pct": _save(crop(0.10), quality=85),
        "border_5pct": _save(ImageOps.expand(image.convert("RGB"), border=int(width * 0.05), fill="white"), quality=85),
        "rotate_2deg": _save(image.rotate(2, expand=False), quality=85),
        "mirror": _save(ImageOps.mirror(image), quality=85),
    }


def strip_diacritics(text: str) -> str:
    plain = "".join(c for c in unicodedata.normalize("NFD", text) if unicodedata.category(c) != "Mn")
    return plain.replace("ł", "l").replace("Ł", "L")


def text_edits(text: str, rng: random.Random) -> dict[str, str]:
    words = text.split()
    edits = {
        "punctuation": text.rstrip(".!") + "!!",
        "strip_diacritics": strip_diacritics(text),
        "append_two_words": text + " bardzo ladne",
    }
    if len(text) > 8:
        at = rng.randrange(2, len(text) - 2)
        edits["one_typo"] = text[:at] + text[at + 1] + text[at] + text[at + 2:]
    if len(words) >= 4:
        swapped = words[:]
        i = rng.randrange(len(swapped) - 1)
        swapped[i], swapped[i + 1] = swapped[i + 1], swapped[i]
        edits["swap_two_words"] = " ".join(swapped)
        dropped = words[:]
        del dropped[rng.randrange(len(dropped))]
        edits["drop_one_word"] = " ".join(dropped)
        middle = len(words) // 2
        edits["swap_halves"] = " ".join(words[middle:] + words[:middle])
    return edits


# -- the measurements -----------------------------------------------------------


def measure(data_dir: Path, image_sample: int, text_sample: int, stream: bool = True) -> dict:
    rng = random.Random(SEED)
    table = pq.read_table(data_dir / "pl.parquet", columns=["image_id", "captions", "image"])
    rows = table.to_pylist()
    photos = [row["image"]["bytes"] for row in rows]
    captions = [row["captions"] for row in rows]
    results: dict = {"dataset": {"photos": len(rows), "captions": sum(len(c) for c in captions)}}

    lengths = [len(text) for group in captions for text in group]
    results["dataset"]["caption_chars"] = {
        "median": float(np.median(lengths)),
        "share_at_least_20": round(float(np.mean([n >= 20 for n in lengths])), 4),
        "share_at_least_60": round(float(np.mean([n >= 60 for n in lengths])), 4),
        "share_at_least_100": round(float(np.mean([n >= 100 for n in lengths])), 4),
    }

    # 1. The whole stage in shadow mode over honest submissions, each from a
    #    different contributor: what every filter would have done.
    pipeline = build_pipeline(PipelineConfig(default_mode=FilterMode.SHADOW))
    pipeline.preflight()
    start = datetime(2026, 10, 1, tzinfo=timezone.utc)
    would: Counter = Counter()
    errors: Counter = Counter()
    for index, (photo, group) in enumerate(zip(photos, captions) if stream else ()):
        outcome = pipeline.run_pre_filters(
            Interaction(
                submission_id=f"xm-{index}",
                campaign_id="calibration",
                user_id=f"author-{index}",
                image=NewImage(photo),
                text=group[0],
                submitted_at=start + timedelta(minutes=index),
            )
        )
        for record in outcome.records:
            if record.error:
                errors[record.filter_name] += 1
            elif record.result is not None and record.result.reason_code:
                would[f"{record.filter_name}|{record.result.verdict.value}|{record.result.reason_code}"] += 1
    results["honest_stream_shadow"] = {
        "submissions": len(photos) if stream else 0,
        "would": dict(sorted(would.items())),
        "errors": dict(errors),
    }

    # 2. Photos: distances between different honest photos (false positives)...
    photo_hashes = [dhash(photo) for photo in photos]
    histogram, nearest = all_pair_distances(photo_hashes)
    results["image_different_photos"] = {
        "pairs": int(histogram.sum()),
        "pairs_at_or_below": at_or_below(histogram),
        "photos_with_a_neighbour_at_or_below": {
            str(t): int((nearest <= t).sum()) for t in range(MAX_THRESHOLD + 1)
        },
        "nearest_neighbour": summary(nearest.tolist()),
    }

    # ...and between a photo and simple copies of it (what a threshold catches).
    # The same two questions for a 256-bit fingerprint (dhash size 16), which
    # the filter does not use today: is a wider hash worth building?
    wide_hashes = [dhash(photo, size=16) for photo in photos]
    wide = wide_pair_histogram(wide_hashes, 256)
    results["image_different_photos_256_bit"] = {
        "pairs_at_or_below": {str(t): sum(wide[: t + 1]) for t in range(0, 81, 4)},
    }

    edits: dict[str, list[int]] = {}
    wide_edits: dict[str, list[int]] = {}
    for index in rng.sample(range(len(photos)), image_sample):
        for name, edited in image_edits(photos[index]).items():
            edits.setdefault(name, []).append(bin(photo_hashes[index] ^ dhash(edited)).count("1"))
            wide_edits.setdefault(name, []).append((wide_hashes[index] ^ dhash(edited, size=16)).bit_count())
    results["image_copies"] = {
        name: {"distance": summary(d), "caught_at_or_below": share_at_or_below(d)}
        for name, d in edits.items()
    }
    results["image_copies_256_bit"] = {
        name: {
            "distance": summary(d),
            "caught_at_or_below": {str(t): round(float((np.array(d) <= t).mean()), 4) for t in range(0, 81, 4)},
        }
        for name, d in wide_edits.items()
    }

    # 3. Captions: two people describing the same photo (the honest cross-user
    #    case the filter must not punish), restricted like the filter is.
    same_photo = [
        (bin(simhash(a) ^ simhash(b)).count("1"), min(len(a), len(b)))
        for group in captions
        if len(group) == 2
        for a, b in [group]
    ]
    long_enough = [d for d, shortest in same_photo if shortest >= 60]
    results["text_same_photo_two_authors"] = {
        "all": {"distance": summary([d for d, _ in same_photo]), "share_at_or_below": share_at_or_below([d for d, _ in same_photo])},
        "both_at_least_60_chars": {"distance": summary(long_enough), "share_at_or_below": share_at_or_below(long_enough)},
        "identical_text_pairs": sum(1 for group in captions if len(group) == 2 and group[0] == group[1]),
    }

    # Captions of different photos, at least 20 characters (what one honest
    # contributor's own captions look like to the same-user rule).
    usable = [text for group in captions for text in group if len(text) >= 20]
    text_histogram, text_nearest = all_pair_distances([simhash(text) for text in usable])
    results["text_different_photos_min_20_chars"] = {
        "captions": len(usable),
        "pairs": int(text_histogram.sum()),
        "pairs_at_or_below": at_or_below(text_histogram),
        "captions_with_a_neighbour_at_or_below": {
            str(t): int((text_nearest <= t).sum()) for t in range(MAX_THRESHOLD + 1)
        },
    }

    # The cross-user rule only compares captions of at least 60 characters.
    long_captions = [text for group in captions for text in group if len(text) >= 60]
    long_histogram, _ = all_pair_distances([simhash(text) for text in long_captions])
    results["text_different_photos_min_60_chars"] = {
        "captions": len(long_captions),
        "pairs": int(long_histogram.sum()),
        "pairs_at_or_below": at_or_below(long_histogram),
    }

    # Edits of one caption (template reuse by one contributor).
    text_results: dict[str, list[int]] = {}
    for text in rng.sample(usable, text_sample):
        original = simhash(text)
        for name, edited in text_edits(text, rng).items():
            text_results.setdefault(name, []).append(bin(original ^ simhash(edited)).count("1"))
    results["text_edits"] = {
        name: {"distance": summary(d), "caught_at_or_below": share_at_or_below(d)}
        for name, d in text_results.items()
    }
    return results


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--data", type=Path, required=True)
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--image-sample", type=int, default=400)
    parser.add_argument("--text-sample", type=int, default=1000)
    parser.add_argument("--skip-stream", action="store_true", help="skip the slow whole-stage pass")
    args = parser.parse_args()
    results = measure(args.data, args.image_sample, args.text_sample, stream=not args.skip_stream)
    args.out.write_text(json.dumps(results, indent=2, ensure_ascii=False), encoding="utf-8")
    print(f"wrote {args.out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
