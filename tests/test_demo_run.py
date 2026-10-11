"""The recorded demo must keep telling the story it promises."""

from __future__ import annotations

import importlib.util
import pathlib
import sys

import pytest

pytest.importorskip("fastapi", reason="the demo needs the optional `api` extra")

from fastapi.testclient import TestClient  # noqa: E402

from s2e_validator.api import ValidatorService  # noqa: E402
from s2e_validator.api.app import create_app  # noqa: E402
from s2e_validator.judge import RUBRIC_V1, StubJudge  # noqa: E402
from s2e_validator.prefilters import build_pipeline, load_config  # noqa: E402
from s2e_validator.settlement import BatchSubmitter, DryRunChain, InMemoryBatchLog, ScoreMapping  # noqa: E402

VALIDATOR = pathlib.Path(__file__).resolve().parents[1]
API_KEY = "demo-test-" + "k" * 32
SIGNER = "1" * 32


def load_demo():
    spec = importlib.util.spec_from_file_location("run_demo", VALIDATOR / "demo" / "run_demo.py")
    module = importlib.util.module_from_spec(spec)
    # Registered first: dataclasses look their own module up while it loads.
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def demo():
    return load_demo()


@pytest.fixture
def post():
    log = InMemoryBatchLog()
    service = ValidatorService(
        pipeline=build_pipeline(load_config(VALIDATOR / "config" / "demo_campaign.json")),
        judge=StubJudge(RUBRIC_V1),
        submitter=BatchSubmitter(DryRunChain(SIGNER), log, SIGNER),
        batch_log=log,
        mapping=ScoreMapping(max_units_per_submission=100, units_per_point=1),
    )
    app = create_app(service, api_key=API_KEY, settled_on="dry-run", settle_interval=None)
    with TestClient(app) as client:

        def send(wallet, description, photo):
            response = client.post(
                "/submissions",
                data={"wallet": wallet, "description": description},
                files={"photo": ("photo.jpg", photo, "image/jpeg")},
                headers={"Authorization": f"Bearer {API_KEY}"},
            )
            return response.status_code, response.json()

        yield send


def test_every_step_ends_the_way_the_demo_says(demo, post):
    plan = demo.steps(
        demo.placeholder_photo(0), demo.placeholder_photo(1), demo.demo_wallet("a"), demo.demo_wallet("b")
    )
    printed: list[str] = []

    answers = demo.play(post, plan, out=printed.append)

    assert [a["state"] for a in answers] == [step.expect for step in plan]
    assert [(a["rejected_by"], a["reason_code"]) for a in answers[:2]] == [
        ("text_format", "TEXT_REPEATED_CHARACTERS"),
        ("image_format", "IMAGE_UNDECODABLE"),
    ]
    assert answers[3]["reason_code"] == "DUPLICATE_IMAGE_SAME_USER"
    assert answers[4]["reason_code"] == "NEAR_DUPLICATE_IMAGE_TEXT_ONLY"
    assert [a["reward_display"] for a in answers if a["state"] == "paid"] == ["0.000075", "0.000075"]
    text = "\n".join(printed)
    assert "EXPECTED" not in text
    assert "6 submissions: 2 paid, 4 stopped or not rewarded." in text
    assert "This was a dry run: nothing was sent to Solana." in text


def test_the_two_placeholder_photos_are_different_pictures(demo, post):
    """Otherwise step 6 would be caught as a copy of step 3."""
    from s2e_validator.prefilters import dhash, hamming

    a, b = demo.placeholder_photo(0), demo.placeholder_photo(1)

    assert hamming(dhash(a), dhash(b)) > 12
    assert hamming(dhash(a), dhash(demo.resaved(a))) <= 6
