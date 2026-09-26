"""Issue #16 scenario table.

The "must pass" rows matter as much as the rejects: honest contributors
photographing one landmark, or writing short captions that collide, must never
be punished for it.
"""

import threading
import unicodedata
from datetime import timedelta

import pytest

from s2e_validator.prefilters import (
    ExistingImage,
    HashWindow,
    ImageNearDuplicateFilter,
    ImageNearDuplicateSettings,
    InMemorySuspicionLog,
    NewImage,
    TextNearDuplicateFilter,
    TextNearDuplicateSettings,
    Verdict,
    dhash,
    hamming,
    simhash,
)
from s2e_validator.prefilters.hashing import text_contribution_key
from s2e_validator.prefilters.near_duplicate import (
    REASON_IMAGE_SAME_USER,
    REASON_IMAGE_TEXT_ONLY,
    REASON_TEXT_COPIED,
    REASON_TEXT_TEMPLATE,
    REASON_TEXT_UNCOMPARABLE,
)

from helpers import CASTLE, CASTLE_NO_DIACRITICS, CASTLE_TYPO, PIEROGI, make_interaction, photo, recompressed


def run(duplicate_filter, **overrides):
    """check, then commit unless rejected — as the orchestrator will."""
    interaction = make_interaction(**overrides)
    result = duplicate_filter.check(interaction)
    if result.verdict is not Verdict.REJECT:
        revised = duplicate_filter.commit(interaction)
        if revised is not None:
            result = revised
    return result


@pytest.fixture
def image_filter() -> ImageNearDuplicateFilter:
    return ImageNearDuplicateFilter()


@pytest.fixture
def text_filter() -> TextNearDuplicateFilter:
    return TextNearDuplicateFilter()


IMAGE_BAR = ImageNearDuplicateSettings().max_distance
TEXT_BAR = TextNearDuplicateSettings().same_user_max_distance


class TestPerceptualHash:
    def test_recompressed_and_resized_photo_stays_close(self):
        original = photo(1)
        assert hamming(dhash(original), dhash(recompressed(original))) <= IMAGE_BAR

    def test_different_photos_are_far_apart(self):
        assert hamming(dhash(photo(1)), dhash(photo(5))) > IMAGE_BAR * 2

    def test_identical_bytes_hash_identically(self):
        assert dhash(photo(2)) == dhash(photo(2))


class TestSimHash:
    @pytest.mark.parametrize(
        "variant",
        [
            CASTLE_TYPO,
            CASTLE + ".",  # punctuation only
            "Ceglane mury w porannym słońcu, zamek w Malborku widziany od strony Nogatu",
        ],
    )
    def test_light_edits_stay_close(self, variant):
        assert hamming(simhash(CASTLE), simhash(variant)) <= TEXT_BAR

    def test_unrelated_descriptions_are_far_apart(self):
        assert hamming(simhash(CASTLE), simhash("Kluski śląskie z sosem pieczeniowym")) > TEXT_BAR

    def test_two_honest_descriptions_of_one_landmark_stay_clear_of_the_bar(self):
        """The measurement the same-user threshold is calibrated against."""
        first = "Zamek w Malborku, największa ceglana twierdza w Europie, widok od rzeki"
        second = "Malbork - potężny zamek krzyżacki z czerwonej cegły, oglądany z brzegu Nogatu"
        assert hamming(simhash(first), simhash(second)) > TEXT_BAR

    def test_stripped_diacritics_sit_above_the_bar(self):
        """Documented gap: this edit is left to the judge.

        Catching it would mean a threshold close enough to honest rewordings to
        start rejecting real contributors.
        """
        assert hamming(simhash(CASTLE), simhash(CASTLE_NO_DIACRITICS)) > TEXT_BAR

    def test_empty_text_has_no_fingerprint(self):
        assert simhash("") == 0


class TestImageScenarios:
    def test_same_user_reuploading_an_edited_photo_is_rejected(self, image_filter):
        original = photo(1)
        run(image_filter, submission_id="sub-1", user_id="ann", image=NewImage(original))

        result = run(
            image_filter,
            submission_id="sub-2",
            user_id="ann",
            image=NewImage(recompressed(original)),
        )

        assert result.verdict is Verdict.REJECT
        assert result.reason_code == REASON_IMAGE_SAME_USER

    def test_another_users_edited_copy_is_downgraded_not_rejected(self, image_filter):
        original = photo(1)
        run(image_filter, submission_id="sub-1", user_id="ann", image=NewImage(original))

        result = run(
            image_filter,
            submission_id="sub-2",
            user_id="bob",
            image=NewImage(recompressed(original)),
        )

        assert result.verdict is Verdict.FLAG
        assert result.reason_code == REASON_IMAGE_TEXT_ONLY
        assert result.details["contribution"] == "text_only"

    def test_different_photos_of_different_things_pass(self, image_filter):
        run(image_filter, submission_id="sub-1", user_id="ann", image=NewImage(photo(1)))

        result = run(image_filter, submission_id="sub-2", user_id="bob", image=NewImage(photo(7)))

        assert result.verdict is Verdict.PASS

    def test_pooled_photo_descriptions_are_not_compared(self, image_filter):
        run(image_filter, submission_id="sub-1", user_id="ann", image=ExistingImage("img-1"))

        result = run(image_filter, submission_id="sub-2", user_id="ann", image=ExistingImage("img-1"))

        assert result.verdict is Verdict.PASS

    def test_revalidating_one_submission_is_not_a_duplicate(self, image_filter):
        interaction = make_interaction(submission_id="sub-1", image=NewImage(photo(1)))
        image_filter.commit(interaction)

        assert image_filter.check(interaction).verdict is Verdict.PASS

    def test_threshold_is_configurable(self):
        strict = ImageNearDuplicateFilter(ImageNearDuplicateSettings(max_distance=0))
        original = photo(1)
        run(strict, submission_id="sub-1", user_id="ann", image=NewImage(original))

        result = run(
            strict, submission_id="sub-2", user_id="ann", image=NewImage(recompressed(original))
        )

        assert result.verdict is Verdict.PASS  # only byte-identical photos match at 0

    def test_cross_user_match_reaches_the_suspicion_log(self):
        log = InMemorySuspicionLog()
        image_filter = ImageNearDuplicateFilter(suspicion_log=log)
        original = photo(1)
        run(image_filter, submission_id="sub-1", user_id="ann", image=NewImage(original))

        run(
            image_filter,
            submission_id="sub-2",
            user_id="bob",
            image=NewImage(recompressed(original)),
        )

        assert len(log) == 1
        assert log.events[0].duplicate_user_id == "bob"

    def test_same_user_match_is_not_logged_as_suspicion(self):
        log = InMemorySuspicionLog()
        image_filter = ImageNearDuplicateFilter(suspicion_log=log)
        original = photo(1)
        run(image_filter, submission_id="sub-1", user_id="ann", image=NewImage(original))

        run(image_filter, submission_id="sub-2", user_id="ann", image=NewImage(original))

        assert len(log) == 0


class TestTextScenarios:
    def test_same_caption_on_a_different_photo_is_template_farming(self, text_filter):
        run(text_filter, submission_id="sub-1", user_id="ann", image=NewImage(photo(1)), text=CASTLE)

        result = run(
            text_filter, submission_id="sub-2", user_id="ann", image=NewImage(photo(2)), text=CASTLE
        )

        assert result.verdict is Verdict.REJECT
        assert result.reason_code == REASON_TEXT_TEMPLATE

    def test_lightly_reworded_caption_on_a_different_photo_is_caught(self, text_filter):
        run(text_filter, submission_id="sub-1", user_id="ann", image=NewImage(photo(1)), text=CASTLE)

        result = run(
            text_filter,
            submission_id="sub-2",
            user_id="ann",
            image=NewImage(photo(2)),
            text=CASTLE_TYPO,
        )

        assert result.reason_code == REASON_TEXT_TEMPLATE

    def test_one_user_writing_different_captions_passes(self, text_filter):
        run(text_filter, submission_id="sub-1", user_id="ann", image=NewImage(photo(1)), text=CASTLE)

        result = run(
            text_filter,
            submission_id="sub-2",
            user_id="ann",
            image=NewImage(photo(2)),
            text="Kluski śląskie z sosem pieczeniowym na niedzielnym obiedzie",
        )

        assert result.verdict is Verdict.PASS

    def test_long_caption_copied_by_another_user_is_rejected(self, text_filter):
        run(text_filter, submission_id="sub-1", user_id="ann", image=NewImage(photo(1)), text=CASTLE)

        result = run(
            text_filter, submission_id="sub-2", user_id="bob", image=NewImage(photo(2)), text=CASTLE
        )

        assert result.verdict is Verdict.REJECT
        assert result.reason_code == REASON_TEXT_COPIED

    def test_short_identical_captions_from_different_users_pass(self, text_filter):
        """'Pierogi ruskie' is the only sensible description of some photos."""
        run(
            text_filter, submission_id="sub-1", user_id="ann", image=NewImage(photo(1)), text=PIEROGI
        )

        result = run(
            text_filter, submission_id="sub-2", user_id="bob", image=NewImage(photo(2)), text=PIEROGI
        )

        assert result.verdict is Verdict.PASS

    def test_different_users_describing_one_landmark_similarly_pass(self, text_filter):
        run(
            text_filter,
            submission_id="sub-1",
            user_id="ann",
            image=NewImage(photo(1)),
            text="Zamek w Malborku, największa ceglana twierdza w Europie, widok od rzeki",
        )

        result = run(
            text_filter,
            submission_id="sub-2",
            user_id="bob",
            image=NewImage(photo(2)),
            text="Malbork - potężny zamek krzyżacki z czerwonej cegły, oglądany z brzegu Nogatu",
        )

        assert result.verdict is Verdict.PASS

    def test_same_caption_on_the_same_photo_is_left_to_the_image_filters(self, text_filter):
        image = NewImage(photo(1))
        run(text_filter, submission_id="sub-1", user_id="ann", image=image, text=CASTLE)

        result = run(text_filter, submission_id="sub-2", user_id="ann", image=image, text=CASTLE)

        assert result.verdict is Verdict.PASS

    def test_cross_user_length_threshold_is_configurable(self):
        text_filter = TextNearDuplicateFilter(
            TextNearDuplicateSettings(min_cross_user_chars=10)
        )
        run(
            text_filter, submission_id="sub-1", user_id="ann", image=NewImage(photo(1)), text=PIEROGI
        )

        result = run(
            text_filter, submission_id="sub-2", user_id="bob", image=NewImage(photo(2)), text=PIEROGI
        )

        assert result.reason_code == REASON_TEXT_COPIED


class TestSettings:
    def test_image_settings_from_dict(self):
        settings = ImageNearDuplicateSettings.from_dict({"max_distance": 3, "retention_days": 7})
        assert settings.max_distance == 3
        assert settings.retention == timedelta(days=7)

    @pytest.mark.parametrize("raw", [{"max_distance": 65}, {"max_distance": -1}])
    def test_image_settings_reject_impossible_distances(self, raw):
        with pytest.raises(ValueError, match="max_distance"):
            ImageNearDuplicateSettings.from_dict(raw)

    def test_text_settings_from_dict(self):
        settings = TextNearDuplicateSettings.from_dict({"min_cross_user_chars": 120})
        assert settings.min_cross_user_chars == 120

    def test_cross_user_bar_must_not_be_looser_than_same_user(self):
        with pytest.raises(ValueError, match="cross_user_max_distance must be at most"):
            TextNearDuplicateSettings.from_dict(
                {"same_user_max_distance": 2, "cross_user_max_distance": 5}
            )

    def test_unknown_keys_are_rejected(self):
        with pytest.raises(ValueError, match="shingle_size"):
            TextNearDuplicateSettings.from_dict({"shingle": 4})


class TestRetention:
    def test_fingerprints_expire(self):
        now = [make_interaction().submitted_at]
        clock = lambda: now[0]  # noqa: E731
        image_filter = ImageNearDuplicateFilter(
            ImageNearDuplicateSettings(retention=timedelta(days=30)), clock=clock
        )
        original = photo(1)
        run(image_filter, submission_id="sub-1", user_id="ann", image=NewImage(original))

        now[0] = now[0] + timedelta(days=31)

        result = run(image_filter, submission_id="sub-2", user_id="ann", image=NewImage(original))
        assert result.verdict is Verdict.PASS

    def test_caption_fingerprints_expire(self):
        now = [make_interaction().submitted_at]
        clock = lambda: now[0]  # noqa: E731
        text_filter = TextNearDuplicateFilter(
            TextNearDuplicateSettings(retention=timedelta(days=30)), clock=clock
        )
        run(text_filter, submission_id="sub-1", user_id="ann", image=NewImage(photo(1)), text=CASTLE)

        now[0] = now[0] + timedelta(days=31)

        result = run(
            text_filter, submission_id="sub-2", user_id="ann", image=NewImage(photo(2)), text=CASTLE
        )
        assert result.verdict is Verdict.PASS

    @pytest.mark.parametrize("settings_type", [ImageNearDuplicateSettings, TextNearDuplicateSettings])
    def test_retention_days_reaches_the_window(self, settings_type):
        assert settings_type.from_dict({"retention_days": 7}).retention == timedelta(days=7)


class TestInjectedWindow:
    """An empty window is falsy; `window or ...` used to discard it silently."""

    def test_image_filter_writes_to_the_window_it_was_given(self):
        window = HashWindow()
        image_filter = ImageNearDuplicateFilter(window=window)

        run(image_filter, submission_id="sub-1", user_id="ann", image=NewImage(photo(1)))

        assert len(window) == 2  # the owner's bucket and the global one

    def test_text_filter_writes_to_the_window_it_was_given(self):
        window = HashWindow()
        text_filter = TextNearDuplicateFilter(window=window)

        run(text_filter, submission_id="sub-1", user_id="ann", image=NewImage(photo(1)), text=CASTLE)

        assert len(window) == 2


class TestRollback:
    """Called by the stage when a later filter rejects at commit time: the
    rejected submission must leave no fingerprint for its own corrected
    resubmission to collide with."""

    def test_an_image_rollback_empties_both_buckets(self):
        window = HashWindow()
        image_filter = ImageNearDuplicateFilter(window=window)
        rejected = make_interaction(submission_id="sub-1", user_id="ann", image=NewImage(photo(1)))
        image_filter.commit(rejected)

        image_filter.rollback(rejected)

        assert len(window) == 0

    def test_a_text_only_downgrade_leaves_nothing_behind(self):
        """The downgrade records two entries in the copier's bucket, their own
        fingerprint and the original's. Both belong to that submission."""
        window = HashWindow()
        image_filter = ImageNearDuplicateFilter(window=window)
        run(image_filter, submission_id="ann-1", user_id="ann", image=NewImage(photo(1)))
        copier = make_interaction(
            submission_id="bob-1",
            user_id="bob",
            image=NewImage(recompressed(photo(1), quality=30, scale=0.5)),
        )
        image_filter.commit(copier)

        image_filter.rollback(copier)

        # Only Ann's two entries survive.
        assert len(window) == 2

    def test_a_text_rollback_empties_both_buckets(self):
        window = HashWindow()
        text_filter = TextNearDuplicateFilter(window=window)
        rejected = make_interaction(
            submission_id="sub-1", user_id="ann", image=NewImage(photo(1)), text=CASTLE
        )
        text_filter.commit(rejected)

        text_filter.rollback(rejected)

        assert len(window) == 0


class TestStrictnessOrder:
    """A verdict must not depend on whose fingerprint happens to be nearest."""

    def test_repeated_edits_of_another_users_photo_are_capped_at_one(self, image_filter):
        """Measured on these photos: Bob's first edit is 10 bits from Ann's
        original, his second is 3 bits from it but 11 from his own first edit.
        So "is this close to something Bob already has?" misses it; the cap has
        to attach to the photo Bob copied."""
        original = photo(1)
        run(image_filter, submission_id="ann-1", user_id="ann", image=NewImage(original))
        first = run(
            image_filter,
            submission_id="bob-1",
            user_id="bob",
            image=NewImage(recompressed(original, quality=30, scale=0.5)),
        )

        second = run(
            image_filter,
            submission_id="bob-2",
            user_id="bob",
            image=NewImage(recompressed(original, quality=80, scale=0.9)),
        )

        assert first.reason_code == REASON_IMAGE_TEXT_ONLY
        assert second.reason_code == REASON_IMAGE_SAME_USER

    def test_a_cross_user_copy_is_not_masked_by_the_authors_own_caption(self, text_filter):
        """Ann's own earlier caption on the same photo is the nearest entry, but
        the near-identical caption Bob wrote first must still count."""
        photo_a = NewImage(photo(1))
        run(text_filter, submission_id="bob-1", user_id="bob", image=NewImage(photo(3)), text=CASTLE)
        run(text_filter, submission_id="ann-1", user_id="ann", image=photo_a, text=CASTLE_TYPO)

        result = run(
            text_filter, submission_id="ann-2", user_id="ann", image=photo_a, text=CASTLE_TYPO
        )

        assert result.reason_code == REASON_TEXT_COPIED

    def test_a_user_id_of_star_cannot_reach_the_global_bucket(self, image_filter):
        run(image_filter, submission_id="sub-1", user_id="*", image=NewImage(photo(1)))

        result = run(image_filter, submission_id="sub-2", user_id="bob", image=NewImage(photo(1)))

        assert result.reason_code == REASON_IMAGE_TEXT_ONLY  # not a same-user reject


class TestConcurrentCommits:
    """The race the exact-duplicate filter already guards: simultaneous copies."""

    @staticmethod
    def _race(target, contenders):
        results = []
        barrier = threading.Barrier(len(contenders))
        lock = threading.Lock()

        def commit(interaction):
            barrier.wait()
            outcome = target.commit(interaction)
            with lock:
                results.append(outcome)

        threads = [threading.Thread(target=commit, args=(c,)) for c in contenders]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        return results

    def test_only_one_of_many_near_identical_uploads_keeps_the_photo(self):
        image_filter = ImageNearDuplicateFilter()
        original = photo(1)
        contenders = [
            make_interaction(
                submission_id=f"sub-{i}",
                user_id=f"user-{i}",
                image=NewImage(recompressed(original, quality=60 + i)),
            )
            for i in range(16)
        ]

        results = self._race(image_filter, contenders)

        assert sum(result is None for result in results) == 1
        downgraded = [r for r in results if r is not None]
        assert len(downgraded) == 15
        assert {r.reason_code for r in downgraded} == {REASON_IMAGE_TEXT_ONLY}

    def test_only_one_of_many_copied_captions_is_accepted(self):
        text_filter = TextNearDuplicateFilter()
        contenders = [
            make_interaction(
                submission_id=f"sub-{i}",
                user_id=f"user-{i}",
                image=NewImage(photo(100 + i)),
                text=CASTLE,
            )
            for i in range(16)
        ]

        results = self._race(text_filter, contenders)

        assert sum(result is None for result in results) == 1
        assert {r.reason_code for r in results if r is not None} == {REASON_TEXT_COPIED}


class TestBoundedInput:
    def test_a_photo_above_the_pixel_bound_is_not_decoded(self):
        image_filter = ImageNearDuplicateFilter(ImageNearDuplicateSettings(max_pixels=1000))
        interaction = make_interaction(image=NewImage(photo(1)))

        result = image_filter.check(interaction)

        assert result.verdict is Verdict.PASS
        assert result.details["fingerprint"] == "skipped"
        assert image_filter.commit(interaction) is None  # and commit does not raise

    def test_bytes_that_are_not_a_photo_pass_without_raising(self, image_filter):
        interaction = make_interaction(image=NewImage(b"not a photo" * 100))

        assert image_filter.check(interaction).details["fingerprint"] == "skipped"
        assert image_filter.commit(interaction) is None

    def test_an_enormous_caption_is_flagged_not_compared(self, text_filter):
        huge = CASTLE + " " + "x" * 5_000_000

        result = text_filter.check(make_interaction(text=huge))

        assert result.verdict is Verdict.FLAG
        assert result.reason_code == REASON_TEXT_UNCOMPARABLE

    def test_invisible_padding_cannot_empty_a_copied_caption(self, text_filter):
        """8,000 zero-width characters in front of a copy used to canonicalise
        to an empty string: fingerprint 0, length 0, nothing compared, PASS."""
        padded = chr(0x200B) * 8_000 + CASTLE
        run(text_filter, submission_id="sub-1", user_id="ann", image=NewImage(photo(1)), text=CASTLE)

        result = run(
            text_filter, submission_id="sub-2", user_id="bob", image=NewImage(photo(2)), text=padded
        )

        assert result.verdict is not Verdict.PASS
        assert result.reason_code == REASON_TEXT_UNCOMPARABLE

    def test_a_copy_pushed_past_the_limit_is_not_compared_on_a_prefix(self, text_filter):
        """A unique preamble used to push the copied part out of the compared
        prefix. The whole caption is compared or none of it is."""
        preamble = " ".join(f"wstep{i}" for i in range(400))
        assert len(preamble) > 2_000
        run(text_filter, submission_id="sub-1", user_id="ann", image=NewImage(photo(1)), text=CASTLE)

        result = run(
            text_filter,
            submission_id="sub-2",
            user_id="bob",
            image=NewImage(photo(2)),
            text=preamble + " " + CASTLE,
        )

        assert result.reason_code == REASON_TEXT_UNCOMPARABLE

    def test_an_uncomparable_caption_records_nothing(self):
        window = HashWindow()
        text_filter = TextNearDuplicateFilter(window=window)

        text_filter.commit(make_interaction(text=chr(0x200B) * 8_000 + CASTLE))

        assert len(window) == 0

    def test_an_uncomparable_caption_still_gets_a_unique_text_only_key(self):
        padded = chr(0x200B) * 8_000 + CASTLE
        key = text_contribution_key(padded)

        assert key.startswith("text-raw:")
        assert key == text_contribution_key(padded)
        assert key != text_contribution_key(CASTLE)

    def test_hashing_does_not_wait_for_the_window_lock(self):
        """commit hashes first and locks second, so one worker's SimHash never
        blocks another's commit. Proved by preparing a caption while another
        thread holds the lock."""
        window = HashWindow()
        text_filter = TextNearDuplicateFilter(window=window)
        holding, release = threading.Event(), threading.Event()

        def hold_the_lock():
            with window.locked():
                holding.set()
                release.wait(timeout=5)

        holder = threading.Thread(target=hold_the_lock)
        holder.start()
        holding.wait(timeout=5)
        try:
            prepared = text_filter._prepare(make_interaction(text=CASTLE))
        finally:
            release.set()
            holder.join()

        assert prepared.length == len(CASTLE)


class TestInvisibleEdits:
    """Edits a reader cannot see must not make a copy look new."""

    def test_zero_width_characters_do_not_disguise_a_template(self, text_filter):
        laced = "\u200b".join(CASTLE)
        run(text_filter, submission_id="sub-1", user_id="ann", image=NewImage(photo(1)), text=CASTLE)

        result = run(
            text_filter, submission_id="sub-2", user_id="ann", image=NewImage(photo(2)), text=laced
        )

        assert result.reason_code == REASON_TEXT_TEMPLATE

    def test_decomposed_letters_do_not_disguise_a_template(self, text_filter):
        decomposed = unicodedata.normalize("NFD", CASTLE)
        assert decomposed != CASTLE
        run(text_filter, submission_id="sub-1", user_id="ann", image=NewImage(photo(1)), text=CASTLE)

        result = run(
            text_filter,
            submission_id="sub-2",
            user_id="ann",
            image=NewImage(photo(2)),
            text=decomposed,
        )

        assert result.reason_code == REASON_TEXT_TEMPLATE

    def test_invisible_edits_share_one_text_only_key(self):
        variants = [CASTLE, "\u200b".join(CASTLE), unicodedata.normalize("NFD", CASTLE)]
        assert len({text_contribution_key(v) for v in variants}) == 1


class TestContentKeyedCache:
    def test_a_corrected_resubmission_under_the_same_id_is_judged_afresh(self, image_filter):
        """The old cache was keyed on submission id alone, so a new photo under
        the same id reused the old fingerprint."""
        run(image_filter, submission_id="ann-1", user_id="ann", image=NewImage(photo(1)))
        first = make_interaction(submission_id="bob-1", user_id="bob", image=NewImage(photo(9)))
        image_filter.check(first)

        corrected = make_interaction(
            submission_id="bob-1", user_id="bob", image=NewImage(recompressed(photo(1)))
        )

        assert image_filter.check(corrected).reason_code == REASON_IMAGE_TEXT_ONLY


class TestShortCaptionsFromOneUser:
    def test_one_user_may_caption_two_plates_identically(self, text_filter):
        """Short captions collide honestly for one contributor too."""
        run(text_filter, submission_id="sub-1", user_id="ann", image=NewImage(photo(1)), text=PIEROGI)

        result = run(
            text_filter, submission_id="sub-2", user_id="ann", image=NewImage(photo(2)), text=PIEROGI
        )

        assert result.verdict is Verdict.PASS


class TestSettingsValidation:
    @pytest.mark.parametrize(
        "settings_type, raw, message",
        [
            (ImageNearDuplicateSettings, {"max_distance": True}, "whole number"),
            (ImageNearDuplicateSettings, {"max_distance": 10.5}, "whole number"),
            (ImageNearDuplicateSettings, {"max_distance": "10"}, "whole number"),
            (ImageNearDuplicateSettings, {"retention_days": "7"}, "whole number"),
            (ImageNearDuplicateSettings, {"retention_days": 0}, "at least 1"),
            (TextNearDuplicateSettings, {"min_cross_user_chars": "60"}, "whole number"),
            (TextNearDuplicateSettings, {"min_cross_user_chars": -1}, "at least 0"),
            (TextNearDuplicateSettings, {"same_user_max_distance": 100}, "at most 64"),
            (TextNearDuplicateSettings, {"min_same_user_chars": -5}, "at least 0"),
            (TextNearDuplicateSettings, {"shingle_size": 1}, "at least 2"),
        ],
    )
    def test_bad_values_fail_at_load_not_at_runtime(self, settings_type, raw, message):
        with pytest.raises(ValueError, match=message):
            settings_type.from_dict(raw)
