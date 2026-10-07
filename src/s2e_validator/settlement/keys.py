"""Loading the validator's signing key, and refusing to load it carelessly.

This key is what authorises payment: `execute_reward_batch` checks only that
the run's `validator_authority` signed. Anyone holding it can pay any
contributor any share of a run's pool. So the loader is deliberately strict,
and every refusal says what to fix without ever echoing the key file:

- the path must be absolute, and outside every git working tree;
- on Linux and macOS, the file must be private to its owner (chmod 600);
- on Windows, where file permissions cannot be checked here, only a localnet
  key is accepted;
- the key must be the one configured for this network, so a devnet key can
  never sign on mainnet by accident;
- keys that were ever committed to the repository are refused by name.

What this module does *not* check: that the secret half matches the public
half. Python's standard library has no Ed25519, and the signing library the
batch submitter brings (#43, M2) verifies the pair when it loads it.
"""

from __future__ import annotations

import json
import logging
import os
from collections.abc import Mapping
from dataclasses import dataclass
from enum import Enum
from pathlib import Path

from . import base58

logger = logging.getLogger(__name__)

KEYPAIR_BYTES = 64
PUBLIC_KEY_BYTES = 32
#: A Solana keypair file is a JSON array of 64 numbers, about 250 bytes. Reading
#: stops well before anything that size could be something else entirely.
_MAX_FILE_BYTES = 4096

ENV_KEYPAIR_PATH = "S2E_VALIDATOR_KEYPAIR"
ENV_NETWORK = "S2E_NETWORK"
ENV_PUBLIC_KEY = "S2E_VALIDATOR_PUBKEY"

#: Keypairs once committed to aktyz/Survey2Earn. They were removed in #37, but
#: git history keeps them, so their secret halves are readable by anyone with
#: access to the repository. Never sign with them, on any network.
BURNED_PUBLIC_KEYS = frozenset(
    {
        "s2eJ3uDbSAjyNCc3Nv4CUD4rDA8QWMP5d7jkWTPmpZD",
        "s2eevKHmbED85WVPjBrCfMGqArdSbs2UjLjw3bfaYT3",
        "s2egzHcf5HtYKckw3xEotGYzuZQa1DHyDocxAKfCp6v",
        "s2ekAUF6fjveMJakQc5SVjYtxvVe3Gjut2F73Hx6HBj",
    }
)


class Network(str, Enum):
    LOCALNET = "localnet"
    DEVNET = "devnet"
    MAINNET = "mainnet"


class KeyRefused(Exception):
    """The key was not loaded. The message is safe to log: it names paths and
    public keys, never anything from inside the file."""


@dataclass(frozen=True)
class KeySettings:
    """Where the key is and which key it must be.

    `expected_public_key` is the address the run was created with as its
    `validator_authority`. Checking the loaded key against it turns "wrong key
    for this network" from a failed transaction into a refusal at startup.
    """

    path: Path
    network: Network
    expected_public_key: str

    def __post_init__(self) -> None:
        try:
            decoded = base58.decode(self.expected_public_key)
        except ValueError:
            decoded = b""
        if len(decoded) != PUBLIC_KEY_BYTES:
            raise KeyRefused(
                f"{ENV_PUBLIC_KEY} must be a Solana public key (base58, 32 bytes), "
                f"got {self.expected_public_key!r}"
            )

    @classmethod
    def from_env(cls, environ: Mapping[str, str] | None = None) -> KeySettings:
        env = os.environ if environ is None else environ
        missing = [name for name in (ENV_KEYPAIR_PATH, ENV_NETWORK, ENV_PUBLIC_KEY) if not env.get(name)]
        if missing:
            raise KeyRefused(f"set {', '.join(missing)} to start the validator")
        try:
            network = Network(env[ENV_NETWORK])
        except ValueError:
            known = ", ".join(n.value for n in Network)
            raise KeyRefused(f"{ENV_NETWORK} must be one of {known}, got {env[ENV_NETWORK]!r}") from None
        return cls(Path(env[ENV_KEYPAIR_PATH]), network, env[ENV_PUBLIC_KEY])


class ValidatorKey:
    """The loaded key. Prints as its public key and refuses to be copied.

    Everything that could put the secret somewhere unintended, `repr`, `str`,
    an f-string in a log line, pickling it to another process, shows or does
    nothing secret. The bytes come out only through `keypair_bytes()`, which
    exists for the signer and nothing else.
    """

    __slots__ = ("_keypair", "network", "public_key")

    def __init__(self, keypair: bytes, network: Network):
        if len(keypair) != KEYPAIR_BYTES:
            raise ValueError("a Solana keypair is 64 bytes")
        self._keypair = keypair
        self.network = network
        self.public_key = base58.encode(keypair[PUBLIC_KEY_BYTES:])

    def keypair_bytes(self) -> bytes:
        """The 64 raw bytes, for the transaction signer only. Never log them."""
        return self._keypair

    def __repr__(self) -> str:
        return f"ValidatorKey(network={self.network.value}, public_key={self.public_key})"

    __str__ = __repr__

    def __reduce__(self):
        raise TypeError("a validator key is never pickled, copied or sent to another process")


def load_validator_key(settings: KeySettings, *, os_name: str = os.name) -> ValidatorKey:
    """Load the key, or raise KeyRefused saying exactly what to fix."""
    path = settings.path.expanduser()
    if not path.is_absolute():
        # A relative path resolves against the working directory, which for a
        # service started from a checkout is the repository itself.
        raise KeyRefused(f"{ENV_KEYPAIR_PATH} must be an absolute path, got {str(path)!r}")
    path = path.resolve()
    if not path.is_file():
        raise KeyRefused(f"no keypair file at {path}")

    repository = enclosing_git_tree(path.parent)
    if repository is not None:
        raise KeyRefused(
            f"{path} is inside the git working tree at {repository}. Keep keys outside "
            f"every repository, e.g. ~/.config/s2e/{settings.network.value}-validator.json"
        )

    _check_private(path, settings.network, os_name)
    keypair = read_keypair_file(path)
    key = ValidatorKey(keypair, settings.network)

    if key.public_key in BURNED_PUBLIC_KEYS:
        raise KeyRefused(
            f"{key.public_key} was committed to the repository, so its secret is public. "
            "Generate a new keypair."
        )
    if key.public_key != settings.expected_public_key:
        raise KeyRefused(
            f"{path} holds {key.public_key}, but the {settings.network.value} run expects "
            f"{settings.expected_public_key} ({ENV_PUBLIC_KEY}). Wrong key for this network?"
        )

    logger.info(
        "validator_key_loaded network=%s public_key=%s", settings.network.value, key.public_key
    )
    return key


def enclosing_git_tree(directory: Path) -> Path | None:
    """The working tree `directory` sits in, if any.

    `.git` is a directory in a normal clone and a file in a worktree or a
    submodule; both count.
    """
    for candidate in (directory, *directory.parents):
        if (candidate / ".git").exists():
            return candidate
    return None


def _file_mode(path: Path) -> int:
    return path.stat().st_mode


def _check_private(path: Path, network: Network, os_name: str) -> None:
    if os_name == "nt":
        # Windows keeps permissions in ACLs, which the stat mode does not show,
        # so privacy cannot be verified here. Fine for a local test validator;
        # anything that holds real value runs on Linux.
        if network is not Network.LOCALNET:
            raise KeyRefused(
                f"a {network.value} key cannot be loaded on Windows, where file "
                "permissions cannot be verified. Run the validator on Linux."
            )
        logger.warning("validator_key_permissions_unchecked os=windows network=localnet")
        return
    mode = _file_mode(path) & 0o777
    if mode & 0o077:
        raise KeyRefused(
            f"{path} is readable or writable by other users (mode {mode:03o}). "
            f"Run: chmod 600 {path}"
        )


def create_keypair_file(path: Path, keypair: bytes) -> None:
    """Write a new keypair file, private to its owner. Never overwrites: the
    call fails if anything is already at `path`."""
    if len(keypair) != KEYPAIR_BYTES:
        raise ValueError("a Solana keypair is 64 bytes")
    # O_EXCL: never replace a key that appeared in the meantime.
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(descriptor, "w") as handle:
        json.dump(list(keypair), handle)


def read_keypair_file(path: Path) -> bytes:
    """Parse the file. No message here may contain any of its content."""
    with path.open("rb") as handle:
        raw = handle.read(_MAX_FILE_BYTES + 1)
    if len(raw) > _MAX_FILE_BYTES:
        raise KeyRefused(f"{path} is too large to be a Solana keypair file")
    invalid = KeyRefused(
        f"{path} is not a Solana keypair file (expected a JSON array of {KEYPAIR_BYTES} "
        "numbers from 0 to 255, as written by solana-keygen)"
    )
    try:
        values = json.loads(raw)
    except (ValueError, UnicodeDecodeError):
        # `from None`: the decode error's context can quote the document.
        raise invalid from None
    if (
        not isinstance(values, list)
        or len(values) != KEYPAIR_BYTES
        or not all(type(v) is int and 0 <= v <= 255 for v in values)
    ):
        raise invalid
    return bytes(values)
