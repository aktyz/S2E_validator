"""The numbers that decide whether a judge may pay contributors (judge/evaluation.py)."""

from __future__ import annotations

import json

import pytest

from s2e_validator.judge import RUBRIC_V1, StubJudge
from s2e_validator.judge.evaluation import (
    Label,
    LabelError,
    combine,
    held_out_pay_line,
    judged_row,
    load_labels,
    rejected_row,
    summarise,
)

from helpers import item


def row(case="c1", photo="p1.jpg", kind="good", status="SCORED", score=50, rewardable=True,
        expect=True, reasons=(), expect_reasons=(), hold_reason=None):
    return {
        "case": case, "photo": photo, "kind": kind, "expect_rewardable": expect,
        "expect_reasons": expect_reasons, "status": status, "score": score,
        "rewardable": rewardable, "reasons": list(reasons), "criteria": {},
        "hold_reason": hold_reason, "judge": None,
    }


def held(**kwargs):
    return row(status="HELD", score=None, rewardable=None, hold_reason="JUDGE_UNAVAILABLE", **kwargs)


class TestExpectedReasons:
    """F-01 and F-02 of the review of #2."""

    @pytest.mark.parametrize("expected", ["GENERIC_DESCRIPTION", ["GENERIC_DESCRIPTION"]])
    def test_a_string_or_a_list_both_match(self, expected):
        rows = [row(reasons=["GENERIC_DESCRIPTION", "LOW_EFFORT"], expect_reasons=expected)]

        assert summarise(rows)["expected_reason_given"]["given"] == 1

    def test_a_list_needs_every_reason(self):
        rows = [row(reasons=["LOW_EFFORT"], expect_reasons=["LOW_EFFORT", "GENERIC_DESCRIPTION"])]

        assert summarise(rows)["expected_reason_given"]["given"] == 0

    def test_a_hold_is_counted_apart_not_as_a_miss(self):
        rows = [
            row(case="a", reasons=["GENERIC_DESCRIPTION"], expect_reasons="GENERIC_DESCRIPTION"),
            held(case="b", photo="p2.jpg", expect_reasons="GENERIC_DESCRIPTION"),
        ]

        assert summarise(rows)["expected_reason_given"] == {
            "cases": 1, "given": 1, "held": 1, "stopped_before_the_judge": 0,
        }

    def test_a_case_stopped_before_the_judge_is_counted_apart(self):
        rows = [row(status="REJECTED", score=0, rewardable=False, reasons=["TEXT_ADDRESSES_GRADER"],
                    expect=False, expect_reasons="INSTRUCTIONS_TO_JUDGE")]

        assert summarise(rows)["expected_reason_given"]["stopped_before_the_judge"] == 1
        assert summarise(rows)["expected_reason_given"]["cases"] == 0


class TestAgreement:
    def test_holds_are_neither_right_nor_wrong(self):
        rows = [row(case="a"), held(case="b", photo="p2.jpg"), row(case="c", photo="p3.jpg", rewardable=True, expect=False)]

        assert summarise(rows)["overall"] == {"cases": 3, "held": 1, "agree": 1, "rate": 0.5}

    def test_a_text_check_rejection_counts_as_decided(self):
        rows = [row(status="REJECTED", score=0, rewardable=False, expect=False, kind="injection")]

        assert summarise(rows)["per_kind"]["injection"]["agree"] == 1

    def test_good_against_generic_per_photo(self):
        rows = [
            row(case="g1", photo="p1.jpg", kind="good", score=56),
            row(case="x1", photo="p1.jpg", kind="generic", score=11, rewardable=False, expect=False),
            row(case="g2", photo="p2.jpg", kind="good", score=11, rewardable=False),
            row(case="x2", photo="p2.jpg", kind="generic", score=36, expect=False),
        ]

        assert summarise(rows)["good_beats_generic"] == {"photos": 2, "wins": 1}


class TestPayLine:
    def test_the_lowest_line_that_pays_nothing_wrong_on_the_first_half(self):
        # Photos sorted: a (first half), b (second half).
        rows = [
            row(case="1", photo="a.jpg", kind="generic", score=17, rewardable=False, expect=False),
            row(case="2", photo="a.jpg", kind="good", score=36),
            row(case="3", photo="b.jpg", kind="good", score=36),
            row(case="4", photo="b.jpg", kind="generic", score=11, rewardable=False, expect=False),
        ]

        line = held_out_pay_line(rows)

        assert line["line"] == 18
        assert (line["chosen_on"], line["held_out"]) == ("2/2", "2/2")

    def test_a_blocking_reason_is_never_paid_whatever_the_line(self):
        rows = [row(photo="a.jpg", kind="mismatch", score=90, reasons=["DESCRIPTION_MISMATCH"],
                    rewardable=False, expect=False)]

        assert held_out_pay_line(rows)["line"] == 0


class TestLoadingLabels:
    @pytest.fixture
    def photos(self, tmp_path):
        folder = tmp_path / "photos"
        folder.mkdir()
        for name in ("p1.jpg", "p2.jpg"):
            (folder / name).write_bytes(b"jpeg")
        return folder

    def write(self, tmp_path, *labels):
        path = tmp_path / "labels.jsonl"
        path.write_text("\n".join(json.dumps(label) for label in labels) + "\n", encoding="utf-8")
        return path

    def good(self, **overrides):
        return {"case": "c1", "photo": "p1.jpg", "kind": "good", "description": "A red bridge.",
                "expect_rewardable": True, **overrides}

    def test_a_well_formed_file(self, tmp_path, photos):
        labels = load_labels(self.write(tmp_path, self.good(expect_reasons=["LOW_EFFORT"])), photos)

        assert labels == [Label("c1", "p1.jpg", "good", "A red bridge.", True, ("LOW_EFFORT",))]

    @pytest.mark.parametrize(
        "label, message",
        [
            ({"photo": "p1.jpg", "kind": "good", "description": "x", "expect_rewardable": True}, "'case'"),
            ({"case": "c1", "photo": "p1.jpg", "kind": "good", "description": "x", "expect_rewardable": "false"}, "true or false"),
            ({"case": "c1", "photo": "p9.jpg", "kind": "good", "description": "x", "expect_rewardable": True}, "not in"),
            ({"case": "c1", "photo": "p1.jpg", "kind": "good", "description": "x", "expect_rewardable": True, "expect_reasons": [3]}, "expect_reasons"),
        ],
    )
    def test_a_bad_label_stops_before_any_model_is_called(self, tmp_path, photos, label, message):
        with pytest.raises(LabelError, match=message):
            load_labels(self.write(tmp_path, label), photos)

    def test_a_repeated_case_is_refused(self, tmp_path, photos):
        with pytest.raises(LabelError, match="appears twice"):
            load_labels(self.write(tmp_path, self.good(), self.good(photo="p2.jpg")), photos)

    def test_two_cases_of_one_kind_for_one_photo_are_refused(self, tmp_path, photos):
        """F-10: one would silently overwrite the other in the comparison."""
        with pytest.raises(LabelError, match="second 'good'"):
            load_labels(self.write(tmp_path, self.good(), self.good(case="c2")), photos)

    def test_an_empty_file_is_refused(self, tmp_path, photos):
        (tmp_path / "labels.jsonl").write_text("\n", encoding="utf-8")

        with pytest.raises(LabelError, match="no labels"):
            load_labels(tmp_path / "labels.jsonl", photos)


class TestRows:
    LABEL = Label("c1", "p1.jpg", "good", "A red bridge.", True, ("LOW_EFFORT",))

    def test_a_judged_row_carries_the_verdict_and_its_provenance(self):
        verdict = StubJudge(RUBRIC_V1).judge(item())

        built = judged_row(self.LABEL, verdict)

        assert built["status"] == "SCORED"
        assert built["score"] == verdict.quality_score
        assert built["rewardable"] is True
        assert built["judge"]["model"] == "stub"
        assert built["expect_reasons"] == ["LOW_EFFORT"]

    def test_a_rejected_row_is_unpaid_and_says_why(self):
        built = rejected_row(self.LABEL, "TEXT_ADDRESSES_GRADER")

        assert (built["status"], built["rewardable"], built["reasons"]) == ("REJECTED", False, ["TEXT_ADDRESSES_GRADER"])

    def test_the_summary_names_the_judge(self):
        built = judged_row(self.LABEL, StubJudge(RUBRIC_V1).judge(item()))

        assert summarise([built])["judge"]["rubric_version"] == RUBRIC_V1.version


class TestCombining:
    def test_paid_only_where_both_paid(self):
        first = [row(case="a"), row(case="b", photo="p2.jpg")]
        second = [row(case="a"), row(case="b", photo="p2.jpg", rewardable=False)]

        assert [r["rewardable"] for r in combine(first, second)] == [True, False]

    def test_a_hold_in_either_is_a_hold(self):
        combined = combine([row(case="a")], [held(case="a")])

        assert combined[0]["status"] == "HELD" and combined[0]["rewardable"] is None

    def test_different_cases_are_refused(self):
        with pytest.raises(LabelError, match="same cases"):
            combine([row(case="a")], [row(case="b")])
