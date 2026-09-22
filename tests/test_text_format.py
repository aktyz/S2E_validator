import pytest

from s2e_validator.prefilters import TextFormatFilter, TextFormatSettings, Verdict
from s2e_validator.prefilters.text_format import (
    REASON_CONTROL_CHARACTERS,
    REASON_EMPTY,
    REASON_ENCODING,
    REASON_INSTRUCTION_ECHO,
    REASON_LOOKS_TRUNCATED,
    REASON_LOW_VARIETY,
    REASON_REPEATED_CHARACTERS,
    REASON_REPEATED_PATTERN,
    REASON_TOO_LONG,
    REASON_TOO_SHORT,
)

from conftest import make_interaction


def check(text: str, settings: TextFormatSettings | None = None):
    return TextFormatFilter(settings).check(make_interaction(text=text))


def reason(text: str, settings: TextFormatSettings | None = None) -> str | None:
    return check(text, settings).reason_code


class TestValidDescriptions:
    @pytest.mark.parametrize(
        "text",
        [
            "Pierogi ruskie",
            "Zamek w Malborku o zachodzie słońca",
            "Kluski śląskie z sosem pieczeniowym i mięsem",
            "Fontanna Neptuna na Długim Targu w Gdańsku",
            "Zamek",  # exactly min_chars
            "Kościół Mariacki zbudowany w 1863 roku, widok od strony rynku",
            "Bardzo bardzo ładny widok",  # a repeated word is not a repeated pattern
            "Ale ładneeee zdjęcie",  # a short character run is not a flood
        ],
    )
    def test_real_descriptions_pass(self, text):
        assert check(text).verdict is Verdict.PASS

    def test_long_but_valid_description_passes(self):
        text = " ".join(f"szczegol{i}" for i in range(150))[:2000]
        assert check(text).verdict is Verdict.PASS


class TestLength:
    @pytest.mark.parametrize("text", ["", "   ", "\n\t  \r\n"])
    def test_empty_and_whitespace_only(self, text):
        assert reason(text) == REASON_EMPTY

    @pytest.mark.parametrize("text", ["Kot", "abcd", " ok "])
    def test_too_short(self, text):
        assert reason(text) == REASON_TOO_SHORT

    def test_too_long(self):
        text = " ".join(f"szczegol{i}" for i in range(400))
        result = check(text)
        assert result.reason_code == REASON_TOO_LONG
        assert result.details["maximum"] == 2000

    def test_length_uses_collapsed_whitespace(self):
        # Padding does not buy length: "Kot" is 3 characters however it is spaced.
        assert reason("  Kot   \n  ") == REASON_TOO_SHORT

    def test_bounds_are_configurable(self):
        settings = TextFormatSettings(min_chars=20, max_chars=40)
        assert reason("Pierogi ruskie", settings) == REASON_TOO_SHORT
        assert check("Pierogi ruskie z cebulką i śmietaną", settings).verdict is Verdict.PASS


class TestEncoding:
    def test_lone_surrogate_is_rejected(self):
        result = check("Zamek w Malborku \ud800")
        assert result.reason_code == REASON_ENCODING
        assert result.details["cause"] == "surrogates"

    def test_replacement_characters_are_rejected(self):
        result = check("Zamek w Malborku \ufffd\ufffd")
        assert result.reason_code == REASON_ENCODING
        assert result.details["count"] == 2

    def test_polish_diacritics_are_fine(self):
        assert check("Żółta łódź płynie wzdłuż Wisły").verdict is Verdict.PASS

    @pytest.mark.parametrize("text", ["Zamek\x00 krolewski", "Zamek \x07\x07\x07 krolewski"])
    def test_control_characters_are_rejected(self, text):
        assert reason(text) == REASON_CONTROL_CHARACTERS

    def test_the_emoji_string_from_the_readme_passes(self):
        """Regression pin for the #31 review: this exact string was rejected as
        TEXT_CONTROL_CHARACTERS while Unicode category Cf was counted."""
        assert check("Zamek w Malborku \U0001f469\u200d\U0001f4bb").verdict is Verdict.PASS

    @pytest.mark.parametrize(
        "text, note",
        [
            ("Zamek w Malborku ❤\ufe0f\u200d\U0001f525", "ZWJ inside another multi-part emoji"),
            ("Zamek\u200b w Malborku", "zero-width space, common when pasting"),
            ("Zamek\u200c w Malborku", "zero-width non-joiner"),
            ("Zamek kro\u00adlewski w Malborku", "soft hyphen"),
            ("\ufeffZamek w Malborku", "byte order mark"),
            ("Zamek w Malborku ❤\ufe0f", "variation selector"),
        ],
    )
    def test_format_characters_are_not_control_characters(self, text, note):
        """Category Cf rides along with ordinary phone input. Rejecting it would
        punish anyone who used an emoji or pasted text."""
        assert check(text).verdict is Verdict.PASS, note

    def test_newlines_and_tabs_are_allowed(self):
        assert check("Zamek w Malborku\n\tWidok od rzeki").verdict is Verdict.PASS


class TestJunkText:
    @pytest.mark.parametrize("text", ["aaaaaaaa", "Zamekkkkkkkk", "hhhhhhhhhhhh"])
    def test_repeated_letter_runs(self, text):
        assert reason(text) == REASON_REPEATED_CHARACTERS

    @pytest.mark.parametrize("text", ["Zbudowany w 1000000 cegiel", "Wow!!!!!! ladny widok"])
    def test_digits_and_punctuation_do_not_count_as_a_run(self, text):
        """A year, a price or an excited row of marks is odd, not invalid."""
        assert check(text).verdict is Verdict.PASS

    @pytest.mark.parametrize("text", ["asdfasdfasdf", "abcabcabcabc", "lol lol lol lol"])
    def test_repeated_patterns(self, text):
        assert reason(text) == REASON_REPEATED_PATTERN

    def test_keyboard_mash_with_few_distinct_characters_is_flagged(self):
        """A flag, not a reject: real words score just as low (see below)."""
        result = check("asdfasdf")

        assert result.verdict is Verdict.FLAG
        assert result.reason_code == REASON_LOW_VARIETY

    @pytest.mark.parametrize("text", ["Bydgoszczy", "Mississippi", "Sosnowiec"])
    def test_single_word_place_names_are_never_rejected(self, text):
        """"Mississippi" is 11 characters with 4 distinct, the same score as
        "asdfasdf"."""
        assert check(text).verdict is not Verdict.REJECT

    def test_a_repeated_block_is_not_masked_by_a_shorter_one(self):
        """The "ha" run wins on repeats but fails on coverage; the "asdf" block
        meets both. Judging only the best match let this through."""
        assert reason("hahahahahaha asdfasdfasdfasdfasdf") == REASON_REPEATED_PATTERN

    def test_thresholds_are_configurable(self):
        lenient = TextFormatSettings(
            max_repeated_run=20, min_pattern_repeats=99, min_character_variety=0.0
        )
        assert check("aaaaaaaa", lenient).verdict is Verdict.PASS
        assert check("abcabcabcabc", lenient).verdict is Verdict.PASS


class TestInstructionEcho:
    SETTINGS = TextFormatSettings(placeholders=("Opisz zdjęcie", "Dodaj opis"))

    @pytest.mark.parametrize("text", ["Opisz zdjęcie", "opisz  zdjęcie ", "DODAJ OPIS"])
    def test_placeholder_pasted_back_is_rejected(self, text):
        assert reason(text, self.SETTINGS) == REASON_INSTRUCTION_ECHO

    def test_placeholder_used_inside_a_real_description_passes(self):
        text = "Opisz zdjęcie zamku w Malborku, widok od strony rzeki"
        assert check(text, self.SETTINGS).verdict is Verdict.PASS

    def test_no_placeholders_configured_by_default(self):
        assert check("Opisz zdjęcie").verdict is Verdict.PASS


class TestTruncation:
    def test_trailing_hyphen_is_flagged_not_rejected(self):
        result = check("Zamek krolewski w Malborku widziany z-")
        assert result.verdict is Verdict.FLAG
        assert result.reason_code == REASON_LOOKS_TRUNCATED

    @pytest.mark.parametrize("text", ["Sopot - ladny widok o zachodzie", "Pierogi i barszcz -"])
    def test_a_spaced_dash_is_not_a_cut_off_word(self, text):
        assert check(text).verdict is Verdict.PASS

    def test_flag_can_be_switched_off(self):
        settings = TextFormatSettings(flag_truncated=False)
        assert check("Zamek krolewski w Malborku widziany z-", settings).verdict is Verdict.PASS


class TestSettings:
    def test_from_dict_applies_values(self):
        settings = TextFormatSettings.from_dict({"min_chars": 12, "placeholders": ["Opisz"]})
        assert settings.min_chars == 12
        assert reason("Pierogi", settings) == REASON_TOO_SHORT

    @pytest.mark.parametrize(
        "raw, message",
        [
            ({"placeholders": "Opisz zdjecie"}, "list of strings"),
            ({"placeholders": [1, 2]}, "only strings"),
            ({"max_repeated_run": "5"}, "whole number"),
            ({"min_chars": True}, "whole number"),
            ({"min_pattern_coverage": 1.5}, "at most 1"),
            ({"min_character_variety": -0.2}, "at least 0"),
            ({"min_pattern_repeats": 1}, "at least 2"),
            ({"flag_truncated": "yes"}, "true or false"),
        ],
    )
    def test_bad_values_fail_at_load_not_at_runtime(self, raw, message):
        """The loud-failure guarantee has to cover values, not just key names."""
        with pytest.raises(ValueError, match=message):
            TextFormatSettings.from_dict(raw)

    def test_from_dict_rejects_unknown_keys(self):
        with pytest.raises(ValueError, match="min_chars"):
            TextFormatSettings.from_dict({"min_char": 12})

    def test_from_dict_rejects_inverted_bounds(self):
        with pytest.raises(ValueError, match="max_chars must be at least"):
            TextFormatSettings.from_dict({"min_chars": 50, "max_chars": 10})
