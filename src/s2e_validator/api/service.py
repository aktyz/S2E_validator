"""One submission, start to finish: filter, judge, price, settle.

No web framework in here. The HTTP layer (`app.py`) only parses a request and
hands it over, so everything that decides whether someone is paid is testable
without a server.

    submission -> Stage 1 filters -> judge -> score mapping -> reward batch

Each step can end the journey, and the outcome says which one did and why.
"""

from __future__ import annotations

import logging
import threading
import uuid
from collections.abc import Callable
from dataclasses import dataclass, replace
from datetime import datetime
from enum import Enum
from typing import Any

from ..judge import Judge, JudgeInput, JudgeStatus
from ..prefilters import Interaction, NewImage, PreFilterPipeline
from ..clock import utc_now
from ..settlement import BatchLog, BatchStatus, BatchSubmitter, Payable, SequenceConflict
from ..settlement.mapping import ScoreMapping
from .report import FilterReport, JudgeReport, filter_reports

logger = logging.getLogger(__name__)

REASON_NOT_REWARDABLE = "NOT_REWARDABLE"


class RecordingFailed(RuntimeError):
    """An accepted submission's reward could not be written to the batch log,
    so nothing is owed and nothing will be sent."""


class SubmissionIdTaken(ValueError):
    """A caller-chosen id that is already in use: being processed, answered, or
    owed a reward. A second submission under it could be shown the first one's
    outcome, payout included."""


DETAIL_SETTLEMENT_UNAVAILABLE = "settlement is temporarily unavailable; the reward stays queued"
#: Outcomes are kept in memory for the result call. Bounded, oldest out first,
#: so a public endpoint cannot grow the process without limit.
DEFAULT_MAX_OUTCOMES = 50_000


class SubmissionState(str, Enum):
    REJECTED = "rejected"  # stopped by a Stage 1 filter
    HELD = "held"  # the judge could not give a trustworthy answer; a person decides
    NOT_REWARDED = "not_rewarded"  # judged, and below the bar
    BATCHED = "batched"  # accepted; its reward batch has not landed yet
    PAID = "paid"  # the batch landed
    FAILED = "failed"  # accepted, but its batch was rejected on-chain; a person decides


@dataclass(frozen=True)
class SubmissionOutcome:
    submission_id: str
    state: SubmissionState
    #: Why it stopped: a filter's reason code, a hold reason, or a batch failure.
    reason_code: str | None = None
    #: Which Stage 1 filter rejected it, when one did.
    rejected_by: str | None = None
    #: Stage 1 flags that travelled with it, e.g. a text-only contribution.
    flags: tuple[str, ...] = ()
    quality_score: int | None = None
    #: What was sent on-chain as Q_i, in the reward mint's base units.
    reward_units: int | None = None
    #: The same amount, as a decimal number of tokens.
    reward_display: str | None = None
    #: The reward batch's transaction signature, once it landed.
    transaction: str | None = None
    detail: str | None = None
    #: Each Stage 1 check that ran, through the whitelist in report.py.
    filters: tuple[FilterReport, ...] = ()
    #: The judge's model and ratings; None when Stage 1 rejected the submission.
    judge: JudgeReport | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "submission_id": self.submission_id,
            "state": self.state.value,
            "reason_code": self.reason_code,
            "rejected_by": self.rejected_by,
            "flags": list(self.flags),
            "quality_score": self.quality_score,
            "reward_units": self.reward_units,
            "reward_display": self.reward_display,
            "transaction": self.transaction,
            "detail": self.detail,
            "filters": [report.to_dict() for report in self.filters],
            "judge": self.judge.to_dict() if self.judge is not None else None,
        }


class ValidatorService:
    def __init__(
        self,
        pipeline: PreFilterPipeline,
        judge: Judge,
        submitter: BatchSubmitter,
        batch_log: BatchLog,
        mapping: ScoreMapping,
        campaign_id: str = "demo",
        clock: Callable[[], datetime] = utc_now,
        max_outcomes: int = DEFAULT_MAX_OUTCOMES,
    ):
        if max_outcomes < 1:
            raise ValueError("max_outcomes must be at least 1")
        self._max_outcomes = max_outcomes
        self._pipeline = pipeline
        self._judge = judge
        self._submitter = submitter
        self._batch_log = batch_log
        self._mapping = mapping
        self._campaign_id = campaign_id
        self._clock = clock
        self._outcomes: dict[str, SubmissionOutcome] = {}
        #: Ids whose submission is still being checked, judged or settled.
        self._in_flight: set[str] = set()
        self._lock = threading.Lock()

    def preflight(self) -> None:
        """Fail at startup if an enabled filter cannot run (see PreFilter.preflight)."""
        self._pipeline.preflight()

    def submit(
        self, wallet: str, description: str, photo: bytes, submission_id: str | None = None
    ) -> SubmissionOutcome:
        """`wallet` is the contributor's Solana address: who they are to Stage 1
        and where the reward goes. It must already be registered for the
        survey (a UserSurvey account and a token account); the demo site does
        that on a visitor's first submission.

        `submission_id` lets the caller hand the id out before the answer
        arrives; one already in use raises SubmissionIdTaken."""
        submission_id = submission_id or uuid.uuid4().hex
        self._claim(submission_id)
        try:
            interaction = Interaction(
                submission_id=submission_id,
                campaign_id=self._campaign_id,
                user_id=wallet,
                image=NewImage(photo),
                text=description,
                # Stamped here, on receipt: the velocity filter measures rates on
                # this, so it must never be a time the contributor chose.
                submitted_at=self._clock(),
            )
            # Remembered before the id leaves the in-flight set, so a reader
            # never finds it in neither.
            return self._remember(self._process(interaction))
        finally:
            with self._lock:
                self._in_flight.discard(submission_id)

    def is_processing(self, submission_id: str) -> bool:
        """True while the submission is still being checked, judged or settled."""
        with self._lock:
            return submission_id in self._in_flight

    def result(self, submission_id: str) -> SubmissionOutcome | None:
        with self._lock:
            outcome = self._outcomes.get(submission_id)
        if outcome is None or outcome.state is not SubmissionState.BATCHED:
            return outcome
        return self._remember(self._settlement_of(outcome))

    def settle_pending(self) -> None:
        """Send whatever is still queued, e.g. once the run has become active."""
        self._submitter.settle([])

    def _claim(self, submission_id: str) -> None:
        """Mark an id as in flight, refusing one already in use.

        The batch log is read first, outside the lock: it outlives a restart,
        which the outcomes in memory do not. A reward that could not be recorded
        leaves no trace anywhere, so its id may be tried again.
        """
        owed = self._batch_log.record_for(submission_id) is not None
        with self._lock:
            if owed or submission_id in self._outcomes or submission_id in self._in_flight:
                raise SubmissionIdTaken(submission_id)
            self._in_flight.add(submission_id)

    # -- the journey -----------------------------------------------------------

    def _process(self, interaction: Interaction) -> SubmissionOutcome:
        submission_id = interaction.submission_id
        stage = self._pipeline.run_pre_filters(interaction)
        # What Stage 1 flagged, plus what it would have rejected had the
        # filter been enforced. On the demo site most filters only observe, so
        # the second group carries the strongest signals (a re-uploaded photo),
        # and the mapping has to see them.
        flags = tuple(
            dict.fromkeys(
                r.result.reason_code
                for r in (*stage.flags, *stage.shadow_rejections)
                if r.result is not None and r.result.reason_code
            )
        )
        checks = filter_reports(stage.records)
        if not stage.proceeds_to_judge:
            return SubmissionOutcome(
                submission_id,
                SubmissionState.REJECTED,
                reason_code=stage.reason_code,
                rejected_by=stage.decided_by,
                flags=flags,
                filters=checks,
            )

        verdict = self._judge.judge(JudgeInput(interaction, flags))
        judged = JudgeReport.of(verdict)
        if verdict.status is JudgeStatus.HELD:
            return SubmissionOutcome(
                submission_id,
                SubmissionState.HELD,
                reason_code=verdict.hold_reason,
                flags=flags,
                filters=checks,
                judge=judged,
            )
        units = self._mapping.units(verdict)
        if units == 0:
            return SubmissionOutcome(
                submission_id,
                SubmissionState.NOT_REWARDED,
                reason_code=(
                    self._mapping.blocking_flag(flags)
                    or (verdict.reason_codes[0] if verdict.reason_codes else REASON_NOT_REWARDABLE)
                ),
                flags=flags,
                quality_score=verdict.quality_score,
                filters=checks,
                judge=judged,
            )

        accepted = SubmissionOutcome(
            submission_id,
            SubmissionState.BATCHED,
            flags=flags,
            filters=checks,
            judge=judged,
            quality_score=verdict.quality_score,
            reward_units=units,
            reward_display=self._mapping.display(units),
        )
        payable = Payable(submission_id, interaction.user_id, units)
        try:
            self._submitter.enqueue([payable])
        except Exception as exc:  # noqa: BLE001 - a busy lock, a full disk, a locked database
            # Nothing was recorded, so "batched" would be a promise nobody
            # keeps. Say so instead. Stage 1 has already counted this photo,
            # so a plain retry would look like a duplicate: a person has to
            # look at these (the log line carries the id).
            logger.critical("reward_not_recorded id=%s error=%s", submission_id, type(exc).__name__)
            raise RecordingFailed(submission_id) from exc
        try:
            stopped = self._submitter.settle([]).detail
        except SequenceConflict:
            raise
        except Exception:  # noqa: BLE001 - the node being down must not lose the answer
            # The payable is in the batch log (enqueue returned), so the
            # reward is owed and will be sent by the next settle call. The
            # contributor gets their id and "batched".
            logger.exception("settlement_unavailable id=%s", submission_id)
            stopped = DETAIL_SETTLEMENT_UNAVAILABLE
        return self._settlement_of(accepted, stopped=stopped)

    def _settlement_of(
        self, accepted: SubmissionOutcome, stopped: str | None = None
    ) -> SubmissionOutcome:
        """Where this accepted submission's reward batch stands in the log."""
        record = self._batch_log.record_for(accepted.submission_id)
        if record is not None and record.status is BatchStatus.CONFIRMED:
            return replace(accepted, state=SubmissionState.PAID, transaction=record.ticket.signature)
        if record is not None and record.status is BatchStatus.FAILED:
            return replace(
                accepted, state=SubmissionState.FAILED, reason_code="BATCH_REJECTED", detail=record.failure
            )
        return replace(accepted, state=SubmissionState.BATCHED, detail=stopped or accepted.detail)

    def _remember(self, outcome: SubmissionOutcome) -> SubmissionOutcome:
        with self._lock:
            self._outcomes.pop(outcome.submission_id, None)
            self._outcomes[outcome.submission_id] = outcome
            while len(self._outcomes) > self._max_outcomes:
                del self._outcomes[next(iter(self._outcomes))]
        logger.info(
            "submission_%s id=%s reason=%s by=%s score=%s units=%s",
            outcome.state.value,
            outcome.submission_id,
            outcome.reason_code,
            outcome.rejected_by,
            outcome.quality_score,
            outcome.reward_units,
        )
        return outcome

