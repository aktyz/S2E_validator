"""The validator as a service: one submission in, one outcome out (#43, R1).

`service` has no web framework in it and imports with the core package.
`app` needs the optional `api` extra (`pip install ".[api]"`), so it is not
imported here: the pipeline stays installable without a web server.
"""

from .service import (
    REASON_NOT_REWARDABLE,
    RecordingFailed,
    SubmissionOutcome,
    SubmissionState,
    ValidatorService,
)

# Sorted, so adding a name is a one-line diff wherever it belongs.
__all__ = [
    "REASON_NOT_REWARDABLE",
    "RecordingFailed",
    "SubmissionOutcome",
    "SubmissionState",
    "ValidatorService",
]
