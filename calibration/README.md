# Stage 1 thresholds, measured on an external dataset (#27)

**Status: provisional.** These numbers come from a public dataset, not from campaign submissions. They replace guesses made on synthetic photos with measurements on real photos and real Polish captions, and they have to be repeated on real submissions before anything is enforced.

## The data

Polish split of Crossmodal-3600 ([`floschne/xm3600`](https://huggingface.co/datasets/floschne/xm3600), CC BY 4.0): 3,600 photos from around the world, each described independently by one or two people in Polish (7,141 captions). The dataset is not stored in this repository.

What it can tell us:

- how often **honest, independent** submissions would trip a filter (false positives);
- with copies made by the script (re-saved, resized, cropped photos; captions with a typo, swapped words, stripped Polish letters), how many **simple re-uploads and edits** a threshold catches.

What it cannot tell us:

- anything about real farming behaviour;
- anything about timing. There are no timestamps, so `submission_velocity` is **not measured at all** and keeps its original guesses;
- long captions. The median caption here is 53 characters and only 8% reach 100, while the campaign asks for at least 100. The text numbers are a rough guide.

Reproduce with:

```bash
pip install pyarrow   # only this script needs it
python calibration/measure_stage1.py --data <dir with pl.parquet> --out results.json
```

The aggregate output of the run on 2026-10-04 is in `results_xm3600_pl.json`.

## What each filter would have done to 3,600 honest submissions

Every filter in shadow mode, default settings, each photo from a different contributor.

| Filter | Would have | Count | Share |
|---|---|---|---|
| `face_detection` | rejected (`IMAGE_CONTAINS_FACE`) | 886 | 24.6% |
| `image_near_duplicate` | flagged text-only (`NEAR_DUPLICATE_IMAGE_TEXT_ONLY`) | 52 | 1.4% |
| `image_format` | rejected (`IMAGE_RESOLUTION_TOO_LOW`) | 5 | 0.1% |
| `text_format` | rejected (`TEXT_TOO_SHORT`) | 1 | 0.03% |

No filter raised an error on any of the 3,600.

## Photos: `image_near_duplicate.max_distance`

The filter treats two photos as the same when their 64-bit fingerprints differ in at most `max_distance` bits. Among 6,478,200 pairs of **different** photos:

| `max_distance` | Different photos mistaken for copies (pairs) | Chance one honest photo collides in a pool of 3,600 | In a pool of 10,000 |
|---|---|---|---|
| 10 (current) | 304 | 16% | 37% |
| 8 | 115 | 6% | 16% |
| 6 | 31 | 1.7% | 4.7% |
| 4 | 7 | 0.4% | 1.1% |

And what each setting catches, on 400 photos copied ten ways:

| Copy | 10 | 8 | 6 | 4 |
|---|---|---|---|---|
| Re-saved at lower quality, resized to half, converted to PNG | 100% | 100% | 100% | 100% |
| Brightened 15% | 99.5% | 99.5% | 99% | 97% |
| Rotated 2 degrees | 97% | 93% | 77% | 54% |
| Cropped 3% per side | 96% | 86% | 62% | 32% |
| Cropped 10% per side | 5% | 2% | 1% | 0% |
| White border added | 15% | 7% | 2% | 0% |
| Mirrored | 1% | 0% | 0% | 0% |

**Finding.** The current value was chosen on synthetic photos, where re-saving moved a fingerprint by up to 9 bits. On real photos re-saving and resizing move it by at most 5, and almost always by 0 to 2. Meanwhile 10 bits is loose enough that unrelated real photos collide, and the collision chance grows with the size of the pool: at 10,000 stored photos more than one honest submission in three would be downgraded to text-only.

**Recommendation: lower `max_distance` from 10 to 6.** It still catches every re-save, resize and format change, and cuts honest collisions tenfold. It gives up most small crops and rotations, which is the right trade under the rule that a wrongly punished contributor costs more than a missed copy. `config/example_campaign.json` is changed accordingly; the code default is left at 10 until this is agreed.

**A wider fingerprint does not fix it.** A 256-bit version of the same hash was measured too. At the setting with the same number of honest collisions (8 pairs at 32 bits, against 7 pairs at 4 of 64), it catches fewer crops (15% against 32%) and about as many rotations. Crops, borders and mirroring need a different technique, not more bits. That is a known gap, left to the judge.

## Captions across contributors: `cross_user_max_distance` and `min_cross_user_chars`

Two people describing the **same photo** is the case this rule must not punish. Among 538 such pairs where both captions have at least 60 characters, the closest pair is 9 bits apart, so **none** is within the current threshold of 4, and none within 8. Among 4,209,351 pairs of long captions of different photos, none is within 4, one is within 8 and three are within 10.

Two contributors wrote word-for-word the same caption for the same photo twice in 3,541 pairs, both short captions, which is what `min_cross_user_chars` is for.

**Recommendation: keep 4.** It produced no false positive here. The measurements would also support 8, which catches far more lightly edited copies (96% of punctuation-only edits against 58%, 51% of single typos against 8%), but that is a decision for real data: the captions here are shorter than the campaign's.

## Captions from one contributor: `same_user_max_distance`

Among 23,416,746 pairs of captions of different photos (at least 20 characters), 81 are within the current threshold of 10. A contributor with 100 captions has about a 1.7% chance of one honest collision. What 10 catches, on 1,000 captions edited seven ways:

| Edit | Caught at 10 | At 12 |
|---|---|---|
| Punctuation changed | 99% | 100% |
| The two halves swapped | 80% | 92% |
| One typo | 70% | 85% |
| One word dropped | 70% | 84% |
| Two neighbouring words swapped | 68% | 84% |
| Two words appended | 50% | 71% |
| Polish letters stripped (ł to l, ą to a) | 39% | 53% |

**Recommendation: keep 10.** Raising it to 12 catches more edits but triples the honest collisions (231 pairs against 81). Stripped Polish letters remain a known gap. On the short captions here one edit changes a larger share of the text than it would in a 100-character caption, so real recall should be higher than this table.

## Faces: `face_detection`

A face was found in 886 of 3,600 photos (24.6%). These are ordinary photos, so the number says how much an outright ban costs: with the default `min_area_ratio` of 0, which counts distant bystanders, **about one honest photo in four would be rejected**.

**Recommendation: keep it in shadow, and decide `min_area_ratio` with whoever owns the rule.** This was run with OpenCV 5.0 locally, while the bundled model is pinned to OpenCV 4.x in CI, so the exact count should be repeated on 4.x before it is quoted.

## Summary

| Setting | Before | After | Basis |
|---|---|---|---|
| `image_near_duplicate.max_distance` | 10 | **6** (example config) | measured |
| `text_near_duplicate.cross_user_max_distance` | 4 | 4 | measured, no false positive |
| `text_near_duplicate.same_user_max_distance` | 10 | 10 | measured |
| `text_near_duplicate.min_cross_user_chars` | 60 | 60 | measured |
| `face_detection` | shadow | shadow | 24.6% of honest photos contain a face |
| `submission_velocity` thresholds | 12 / 30 | unchanged | **not measurable on this data** |
