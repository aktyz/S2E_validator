"""Judges: the real one wraps a model client; the stub needs no model at all.

The model client is `chat_client.ChatCompletionsClient`, for an open model
the campaign serves on its own GPU through an OpenAI-compatible server. No
contributor photo goes to a commercial API (D5). Everything here is testable,
and everything downstream buildable, with the stub.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Callable, Mapping
from typing import Protocol

from ..prefilters.interaction import ExistingImage, NewImage
from .contract import (
    HOLD_IMAGE_UNAVAILABLE,
    HOLD_JUDGE_UNAVAILABLE,
    HOLD_REPLY_INVALID,
    JudgeInput,
    JudgeReply,
    JudgeReplyInvalid,
    JudgeStatus,
    JudgeVerdict,
    Provenance,
    parse_reply,
)
from .prompt import Prompt, build_prompt, image_mime_type, prompt_version
from .rubric import Rubric

logger = logging.getLogger(__name__)

#: Resolves a pooled photo to its bytes. The judge has to see the photo a
#: description is about, and a reference carries only an id.
ImageLoader = Callable[[ExistingImage], NewImage]


class Judge(Protocol):
    def judge(self, item: JudgeInput) -> JudgeVerdict:
        """Score one submission. Never raises: a failure is a HELD verdict."""
        ...


class ModelClient(Protocol):
    """One model behind one provider's API. `name` goes into provenance.

    Rules for an implementation (J4):

    - Send `prompt.system` as the system message and each part of
      `prompt.content` as user content, through the provider's structured
      message API. Never splice a `TextPart` into a raw chat-template string,
      and never let a tokenizer parse special tokens inside one: the
      description is in there, and `<|im_start|>system` in a description must
      stay ordinary text.
    - Return the reply text. Anything else (None for filtered output, say) is
      treated as a malformed reply.
    - Sampling settings live on the client. Until provenance records them
      separately, put whatever changes the output into `name`.
    """

    name: str

    def complete(self, prompt: Prompt) -> str: ...


class ModelJudge:
    """Asks a model, validates the answer, and holds anything it cannot trust.

    A malformed reply and a client exception are both retried, at once and
    with the same prompt, then the submission is HELD, never scored by
    default. Whether a plain retry helps is unmeasured: with deterministic
    sampling a model may well repeat itself. Sending the parse error back as a
    corrective turn, and backing off on rate limits, belong to the client
    (J4), which knows the provider.
    """

    def __init__(
        self,
        client: ModelClient,
        rubric: Rubric,
        image_loader: ImageLoader | None = None,
        max_attempts: int = 2,
    ):
        if max_attempts < 1:
            raise ValueError("max_attempts must be at least 1")
        self._client = client
        self._rubric = rubric
        self._image_loader = image_loader
        self._max_attempts = max_attempts
        self._provenance = Provenance(
            model=client.name,
            prompt_version=prompt_version(rubric),
            rubric_version=rubric.version,
        )

    def judge(self, item: JudgeInput) -> JudgeVerdict:
        interaction = item.interaction
        image = self._photo(interaction.image)
        mime_type = image_mime_type(image) if image is not None else None
        if image is None or mime_type is None:
            return JudgeVerdict.held(item, self._provenance, HOLD_IMAGE_UNAVAILABLE, attempts=0)

        prompt = build_prompt(self._rubric, image, mime_type, interaction.text)
        hold_reason = HOLD_JUDGE_UNAVAILABLE
        for attempt in range(1, self._max_attempts + 1):
            try:
                raw = self._client.complete(prompt)
            except Exception:  # noqa: BLE001 - any provider failure is a hold, not a crash
                logger.exception(
                    "judge_client_error model=%s submission=%s attempt=%d",
                    self._client.name,
                    interaction.submission_id,
                    attempt,
                )
                hold_reason = HOLD_JUDGE_UNAVAILABLE
                continue
            try:
                reply = parse_reply(raw, self._rubric)
            except JudgeReplyInvalid as invalid:
                logger.warning(
                    "judge_reply_invalid model=%s submission=%s attempt=%d error=%s",
                    self._client.name,
                    interaction.submission_id,
                    attempt,
                    invalid,
                )
                hold_reason = HOLD_REPLY_INVALID
                continue
            return scored(item, reply, self._rubric, self._provenance, attempt)

        return JudgeVerdict.held(item, self._provenance, hold_reason, self._max_attempts)

    def _photo(self, image: NewImage | ExistingImage) -> NewImage | None:
        if isinstance(image, NewImage):
            return image
        if self._image_loader is None:
            return None
        try:
            return self._image_loader(image)
        except Exception:  # noqa: BLE001 - a missing pooled photo is a hold
            logger.exception("judge_image_load_error image=%s", image.image_id)
            return None


class StubJudge:
    """Fixed ratings, no model. For building and testing everything downstream.

    `ratings` may be one mapping for every submission, or a function of the
    input, so a test can script exactly the verdicts it needs.
    """

    def __init__(
        self,
        rubric: Rubric,
        ratings: Mapping[str, int] | Callable[[JudgeInput], Mapping[str, int]] | None = None,
        reason_codes: tuple[str, ...] = (),
    ):
        self._rubric = rubric
        # `is None`, not `or`: an explicitly empty mapping is a scripted
        # mistake to refuse, not a request for the defaults.
        if ratings is None:
            ratings = {name: 4 for name in rubric.criterion_names}
        if not callable(ratings):
            # A fixed script is checked here, so a stub that could never
            # produce a verdict fails when it is built, not inside judge().
            parse_reply(_as_json(ratings, reason_codes), rubric)
        self._ratings = ratings
        self._reason_codes = reason_codes
        self._provenance = Provenance(
            model="stub", prompt_version=prompt_version(rubric), rubric_version=rubric.version
        )

    def judge(self, item: JudgeInput) -> JudgeVerdict:
        ratings = self._ratings(item) if callable(self._ratings) else self._ratings
        # Same validation path as a real reply, so a stub cannot produce a
        # verdict the real judge never could.
        reply = parse_reply(
            _as_json(ratings, self._reason_codes),
            self._rubric,
        )
        return scored(item, reply, self._rubric, self._provenance, attempts=1)


def scored(
    item: JudgeInput,
    reply: JudgeReply,
    rubric: Rubric,
    provenance: Provenance,
    attempts: int,
) -> JudgeVerdict:
    score = rubric.score(reply.criteria)
    return JudgeVerdict(
        submission_id=item.interaction.submission_id,
        status=JudgeStatus.SCORED,
        provenance=provenance,
        quality_score=score,
        is_rewardable=rubric.is_rewardable(score, reply.reason_codes),
        criteria=reply.criteria,
        reason_codes=reply.reason_codes,
        confidence=reply.confidence,
        flags=item.flags,
        attempts=attempts,
    )


def _as_json(ratings: Mapping[str, int], reason_codes: tuple[str, ...]) -> str:
    return json.dumps({"criteria": dict(ratings), "reason_codes": list(reason_codes)})
