"""Make the validator's signing key on the machine that will use it.

    python -m s2e_validator.tools.new_key --out /var/lib/s2e/keys/devnet-validator.json

Prints the public key only. That address is what the survey's creator sets as
the run's `validator_authority`, and what goes in `S2E_VALIDATOR_PUBKEY`. The
secret never leaves the machine: it is not printed, and nothing here sends it
anywhere. The file is created private to its owner and is never overwritten.

Needs the `solana` extra. `solana-keygen new` makes the same kind of file;
this exists so the server does not need the Solana command-line tools.
"""

from __future__ import annotations

import argparse
import sys
from collections.abc import Sequence
from pathlib import Path

from solders.keypair import Keypair

from ..settlement import create_keypair_file, enclosing_git_tree


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Create the validator's signing key.")
    parser.add_argument("--out", type=Path, required=True, help="absolute path of the new key file")
    args = parser.parse_args(argv)

    path = args.out.expanduser()
    if not path.is_absolute():
        parser.error("--out must be an absolute path")
    if path.exists():
        parser.error(f"{path} already exists. A key is never overwritten; choose another path.")
    repository = enclosing_git_tree(path.parent)
    if repository is not None:
        parser.error(f"{path} is inside the git working tree at {repository}. Keep keys outside every repository.")
    if not path.parent.is_dir():
        parser.error(f"{path.parent} does not exist")

    keypair = Keypair()
    create_keypair_file(path, bytes(keypair))
    print(keypair.pubkey())
    return 0


if __name__ == "__main__":
    sys.exit(main())
