"""Start the validator service from environment settings.

    uvicorn --factory s2e_validator.api.main:build_app --host 127.0.0.1 --port 8000

| Variable | Meaning |
|---|---|
| `S2E_CHAIN` | `dry-run` (settles nothing) or `solana`. Required: there is no default, so a deployment can never fall back to pretending. |
| `S2E_PIPELINE_CONFIG` | Path to the campaign config JSON. Required. |
| `S2E_BATCH_LOG` | Path of the SQLite batch log. Required: payouts must survive a restart. |
| `S2E_API_KEY` | What the site's backend sends as `Authorization: Bearer <key>`. Required, at least 32 characters. |
| `S2E_MAX_UNITS_PER_SUBMISSION` | The most one submission can be paid, in base units. Required: there is no default (see settlement/mapping.py). |
| `S2E_UNITS_PER_POINT` | Reward base units per quality point (default 1, see D2 in #43). |
| `S2E_REWARD_DECIMALS` | Decimals of the reward mint (default 6). |

With `S2E_CHAIN=solana`, also (needs the `solana` extra):

| Variable | Meaning |
|---|---|
| `S2E_RPC_URL` | The RPC node, https. May carry an API key; it is never logged. |
| `S2E_PROGRAM_ID` | Address of the deployed Survey2Earn program. |
| `S2E_SURVEY_ID`, `S2E_RUN_ID` | The survey and the one run this service pays from. |
| `S2E_VALIDATOR_KEYPAIR`, `S2E_NETWORK`, `S2E_VALIDATOR_PUBKEY` | The signing key (see settlement/keys.py). |
| `S2E_DUST_TOKEN_ACCOUNT` | Optional. The program authority's token account, if it is not the associated one. |
"""

from __future__ import annotations

import logging
import os
from collections.abc import Mapping
from dataclasses import dataclass, field
from importlib.metadata import PackageNotFoundError
from importlib.metadata import version as installed_version
from pathlib import Path

from fastapi import FastAPI

from ..judge import RUBRIC_V1, StubJudge
from ..prefilters import FilterDependencies, build_pipeline, load_config
from ..settlement import (
    MAX_BATCH_SIZE,
    BatchSubmitter,
    ChainClient,
    KeySettings,
    SqliteBatchLog,
    base58,
    load_validator_key,
)
from ..settlement.dry_run import NETWORK_NAME as DRY_RUN
from ..settlement.dry_run import DryRunChain
from ..settlement.mapping import ScoreMapping
from .app import MIN_API_KEY_CHARS, create_app
from .service import ValidatorService

logger = logging.getLogger(__name__)

#: Stands in for the validator's address in a dry run, where nothing is signed.
_DRY_RUN_SIGNER = base58.encode(bytes(32))
#: The distribution name in pyproject.toml.
_DISTRIBUTION = "s2e-validator"


class SettingsError(ValueError):
    """The environment does not describe a service that can start."""


@dataclass(frozen=True)
class ServiceSettings:
    chain: str
    pipeline_config: Path
    batch_log: Path
    api_key: str = field(repr=False)
    max_units_per_submission: int
    units_per_point: int = 1
    reward_decimals: int = 6

    @classmethod
    def from_env(cls, environ: Mapping[str, str] | None = None) -> ServiceSettings:
        env = os.environ if environ is None else environ
        required = (
            "S2E_CHAIN", "S2E_PIPELINE_CONFIG", "S2E_BATCH_LOG", "S2E_API_KEY",
            "S2E_MAX_UNITS_PER_SUBMISSION",
        )
        missing = [name for name in required if not env.get(name)]
        if missing:
            raise SettingsError(f"set {', '.join(missing)} to start the validator service")
        chain = env["S2E_CHAIN"]
        if chain not in (DRY_RUN, "solana"):
            raise SettingsError(f"S2E_CHAIN must be {DRY_RUN} or solana, got {chain!r}")
        try:
            units = int(env.get("S2E_UNITS_PER_POINT", "1"))
            decimals = int(env.get("S2E_REWARD_DECIMALS", "6"))
            maximum = int(env["S2E_MAX_UNITS_PER_SUBMISSION"])
        except ValueError:
            raise SettingsError(
                "S2E_UNITS_PER_POINT, S2E_REWARD_DECIMALS and S2E_MAX_UNITS_PER_SUBMISSION "
                "must be whole numbers"
            ) from None
        if len(env["S2E_API_KEY"]) < MIN_API_KEY_CHARS:
            # Never echo the key, not even its length.
            raise SettingsError(f"S2E_API_KEY must be at least {MIN_API_KEY_CHARS} characters")
        return cls(
            chain=chain,
            pipeline_config=Path(env["S2E_PIPELINE_CONFIG"]),
            batch_log=Path(env["S2E_BATCH_LOG"]),
            api_key=env["S2E_API_KEY"],
            max_units_per_submission=maximum,
            units_per_point=units,
            reward_decimals=decimals,
        )


def build_service(
    settings: ServiceSettings, environ: Mapping[str, str] | None = None
) -> tuple[ValidatorService, str]:
    """The service and the name of the network it settles on."""
    chain, signer, network, batch_size = _chain(settings, os.environ if environ is None else environ)
    batch_log = SqliteBatchLog(settings.batch_log)
    service = ValidatorService(
        # Stage 1 state is in memory for now; the durable stores are #50.
        pipeline=build_pipeline(load_config(settings.pipeline_config), FilterDependencies()),
        # The demo runs a stub judge by decision: it shows the filters and the
        # settlement, and no contributor content goes to any model.
        judge=StubJudge(RUBRIC_V1),
        # One attempt per call: when a send gets no answer, the request ends
        # as "batched" after the client's confirmation wait (about a minute),
        # and the retry loop resolves it in the background. Otherwise a call
        # could outlive the proxy's timeout while still settling.
        submitter=BatchSubmitter(chain, batch_log, signer, max_batch_size=batch_size, max_attempts=1),
        batch_log=batch_log,
        mapping=ScoreMapping(
            max_units_per_submission=settings.max_units_per_submission,
            units_per_point=settings.units_per_point,
            decimals=settings.reward_decimals,
        ),
    )
    return service, network


def _chain(settings: ServiceSettings, env: Mapping[str, str]) -> tuple[ChainClient, str, str, int]:
    """(client, signer address, network name, users per batch)."""
    if settings.chain == DRY_RUN:
        return DryRunChain(_DRY_RUN_SIGNER), _DRY_RUN_SIGNER, DRY_RUN, MAX_BATCH_SIZE

    names = ("S2E_RPC_URL", "S2E_PROGRAM_ID", "S2E_SURVEY_ID", "S2E_RUN_ID")
    missing = [name for name in names if not env.get(name)]
    if missing:
        raise SettingsError(f"S2E_CHAIN=solana also needs {', '.join(missing)}")
    try:
        survey_id, run_id = int(env["S2E_SURVEY_ID"]), int(env["S2E_RUN_ID"])
    except ValueError:
        raise SettingsError("S2E_SURVEY_ID and S2E_RUN_ID must be whole numbers") from None
    try:
        # Imported here: the signing library is an optional extra.
        from ..settlement import solana
    except ImportError:
        raise SettingsError('S2E_CHAIN=solana needs the solana extra: pip install ".[solana]"') from None

    key = load_validator_key(KeySettings.from_env(env))
    chain = solana.SolanaChain(
        solana.JsonRpc(env["S2E_RPC_URL"]),
        key,
        env["S2E_PROGRAM_ID"],
        survey_id,
        run_id,
        dust_token_account=env.get("S2E_DUST_TOKEN_ACCOUNT") or None,
    )
    # Read the run once before serving: a wrong program, survey, run or key
    # should stop the service here, not surface as rewards that never leave
    # the queue.
    state = chain.run_state()
    if state.validator_authority != key.public_key:
        raise SettingsError(
            f"run {run_id} of survey {survey_id} is paid by {state.validator_authority}, "
            f"not by this service's key {key.public_key}"
        )
    if state.budget is not None and settings.max_units_per_submission > state.budget:
        # Otherwise every reward would wait as "batched" until the run closes,
        # with no error anywhere.
        raise SettingsError(
            f"S2E_MAX_UNITS_PER_SUBMISSION is {settings.max_units_per_submission}, but run {run_id} "
            f"can only pay {state.budget} more base units. Lower the payout scale or fund the run."
        )
    logger.info(
        "chain_ready network=%s program=%s survey=%d run=%d expected_sequence=%d run_active=%s",
        key.network.value, env["S2E_PROGRAM_ID"], survey_id, run_id,
        state.expected_sequence, state.run_active,
    )
    return chain, key.public_key, key.network.value, solana.MAX_USERS_PER_TRANSACTION


def service_version() -> str:
    """The installed package's version (pyproject.toml), refreshed by the
    deploy's `pip install`; "unknown" when running from a plain checkout."""
    try:
        return installed_version(_DISTRIBUTION)
    except PackageNotFoundError:
        return "unknown"


def build_app() -> FastAPI:
    """Application factory for uvicorn."""
    # uvicorn configures only its own loggers; without this the validator's
    # own lines (rejections, batches, settlement halts) would go nowhere.
    logging.basicConfig(
        level=os.environ.get("S2E_LOG_LEVEL", "INFO"),
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    settings = ServiceSettings.from_env()
    service, network = build_service(settings)
    return create_app(service, api_key=settings.api_key, settled_on=network, version=service_version())
