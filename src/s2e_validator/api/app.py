"""The HTTP face of the validator: two calls and a health check.

    POST /submissions        a photo, its description and a wallet -> the outcome
    GET  /submissions/{id}   the outcome again, e.g. once a waiting batch landed;
                             202 while it is still being checked
    GET  /healthz            for the uptime monitor; needs no key

**Server to server.** The demo site's backend calls this and holds the API
key; a browser never does, so there is no CORS here. Limits per visitor
(address, session) are the site's job: every request here arrives from the
site's own address.

Thin on purpose. Everything that decides an outcome lives in `service.py`;
this file only bounds what comes in and turns outcomes into JSON.
"""

from __future__ import annotations

import asyncio
import hmac
import logging
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager

from fastapi import Depends, FastAPI, File, Form, Header, HTTPException, Request, UploadFile
from fastapi.responses import JSONResponse
from starlette.concurrency import run_in_threadpool

from ..settlement import SequenceConflict, base58
from .service import RecordingFailed, SubmissionIdTaken, ValidatorService

logger = logging.getLogger(__name__)

#: Above any phone photo (image_format rejects over 25 MiB anyway), below
#: anything that could exhaust memory. Enforced while reading, not after.
DEFAULT_MAX_PHOTO_BYTES = 26 * 1024 * 1024
#: text_format rejects long descriptions with a reason; this only stops a
#: multi-megabyte form field before it is ever decoded.
MAX_DESCRIPTION_CHARS = 20_000
#: A Solana address is 32 to 44 base58 characters.
MAX_WALLET_CHARS = 44
#: A caller-chosen id has the form this service makes itself: uuid4().hex.
SUBMISSION_ID_PATTERN = r"^[0-9a-f]{32}$"
#: Short keys are guessable; a generated one is far longer than this.
MIN_API_KEY_CHARS = 32
_READ_CHUNK = 1024 * 1024
#: How often waiting rewards are retried without anyone asking, in seconds.
DEFAULT_SETTLE_INTERVAL = 30.0


def create_app(
    service: ValidatorService,
    api_key: str,
    max_photo_bytes: int = DEFAULT_MAX_PHOTO_BYTES,
    settled_on: str = "devnet",
    settle_interval: float | None = DEFAULT_SETTLE_INTERVAL,
    version: str = "unknown",
) -> FastAPI:
    """`api_key` is what the site's backend sends as `Authorization: Bearer
    <key>`. `settled_on` names the network in responses, so a dry run can
    never be read as a payout. Every `settle_interval` seconds, waiting
    rewards are tried again; None turns that off. `version` is reported by
    the health check, so the site can show which service it talks to."""
    if len(api_key) < MIN_API_KEY_CHARS:
        raise ValueError(f"the API key must be at least {MIN_API_KEY_CHARS} characters")
    expected = f"Bearer {api_key}".encode()

    def authorised(authorization: str | None = Header(default=None)) -> None:
        # compare_digest: the time taken must not reveal how much of a guess matched.
        if authorization is None or not hmac.compare_digest(authorization.encode(), expected):
            raise HTTPException(status_code=401, detail="missing or wrong API key")

    @asynccontextmanager
    async def lifespan(_: FastAPI) -> AsyncIterator[None]:
        # Refuse to start with a filter that cannot run, rather than fail open
        # on every submission for a whole campaign.
        service.preflight()
        retries = (
            asyncio.create_task(settle_periodically(service, settle_interval))
            if settle_interval is not None
            else None
        )
        try:
            yield
        finally:
            if retries is not None:
                retries.cancel()

    # No interactive docs: they would describe the API to anyone who finds it.
    app = FastAPI(
        title="Survey2Earn validator", version=version, lifespan=lifespan,
        docs_url=None, redoc_url=None, openapi_url=None,
    )

    @app.exception_handler(SequenceConflict)
    async def sequence_conflict(_: Request, exc: SequenceConflict) -> JSONResponse:
        # Something else is signing with the run's key. Stop paying, loudly.
        logger.critical("settlement_halted %s", exc)
        return JSONResponse(status_code=503, content={"detail": "settlement is halted"})

    @app.exception_handler(RecordingFailed)
    async def recording_failed(_: Request, exc: RecordingFailed) -> JSONResponse:
        # Not "batched": nothing was written, so nothing is owed.
        return JSONResponse(
            status_code=503,
            content={"detail": "the reward could not be recorded; nothing is owed", "submission_id": str(exc)},
        )

    @app.exception_handler(SubmissionIdTaken)
    async def submission_id_taken(_: Request, exc: SubmissionIdTaken) -> JSONResponse:
        # Never a second submission under one id: the first may already be paid.
        return JSONResponse(status_code=409, content={"detail": "submission_id already used"})

    @app.get("/healthz")
    def healthz() -> dict[str, str]:
        return {"status": "ok", "settled_on": settled_on, "version": version}

    @app.post("/submissions", dependencies=[Depends(authorised)])
    async def submit(
        photo: UploadFile = File(..., description="The contributor's photo."),
        description: str = Form(..., max_length=MAX_DESCRIPTION_CHARS),
        wallet: str = Form(..., min_length=32, max_length=MAX_WALLET_CHARS),
        submission_id: str | None = Form(
            default=None,
            pattern=SUBMISSION_ID_PATTERN,
            description="Optional. The caller's own id, so it can poll before the answer arrives.",
        ),
    ) -> dict:
        if not _is_solana_address(wallet):
            raise HTTPException(status_code=422, detail="wallet must be a Solana address")
        data = await _read_bounded(photo, max_photo_bytes)
        # The service is synchronous and CPU-bound (decoding, hashing, face
        # detection), so it runs in the worker pool, not on the event loop.
        outcome = await run_in_threadpool(service.submit, wallet, description, data, submission_id)
        return {**outcome.to_dict(), "settled_on": settled_on}

    @app.get("/submissions/{submission_id}", dependencies=[Depends(authorised)], response_model=None)
    def result(submission_id: str) -> dict | JSONResponse:
        # In flight first, then the outcome: `submit` stores the outcome before
        # the id leaves the in-flight set, so a submission that finishes between
        # the two reads is still found. The other order could answer 404.
        if service.is_processing(submission_id):
            # The caller chose this id and is polling before the POST answered.
            return JSONResponse(status_code=202, content={"submission_id": submission_id, "status": "processing"})
        outcome = service.result(submission_id)
        if outcome is not None:
            return {**outcome.to_dict(), "settled_on": settled_on}
        raise HTTPException(status_code=404, detail="unknown submission")

    return app


def _is_solana_address(text: str) -> bool:
    try:
        return len(base58.decode(text)) == 32
    except ValueError:
        return False


async def settle_periodically(
    service: ValidatorService,
    interval: float,
    sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
) -> None:
    """Retry waiting rewards until cancelled: a run that became active, or a
    node that came back, needs nobody to notice."""
    while True:
        await sleep(interval)
        try:
            await run_in_threadpool(service.settle_pending)
        except SequenceConflict as exc:
            # Something else is signing with the run's key. Retrying cannot
            # fix that; stop, and leave the queue for a person.
            logger.critical("settlement_halted %s", exc)
            return
        except Exception:  # noqa: BLE001 - one bad round must not end the retries
            logger.exception("settle_pending_failed")


async def _read_bounded(upload: UploadFile, limit: int) -> bytes:
    """Read an upload, refusing it as soon as it passes `limit`."""
    chunks: list[bytes] = []
    total = 0
    while chunk := await upload.read(_READ_CHUNK):
        total += len(chunk)
        if total > limit:
            raise HTTPException(status_code=413, detail=f"photo larger than {limit} bytes")
        chunks.append(chunk)
    return b"".join(chunks)
