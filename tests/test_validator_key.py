"""Loading the validator's signing key (#43, M3)."""

from __future__ import annotations

import copy
import json
import logging
import pickle
import random
from pathlib import Path

import pytest

from s2e_validator.settlement import (
    BURNED_PUBLIC_KEYS,
    ENV_KEYPAIR_PATH,
    ENV_NETWORK,
    ENV_PUBLIC_KEY,
    KeyRefused,
    KeySettings,
    Network,
    enclosing_git_tree,
    load_validator_key,
)
from s2e_validator.settlement import base58, keys

POSIX = "posix"


def keypair(seed: int = 1, public: bytes | None = None) -> bytes:
    """64 bytes shaped like a Solana keypair: secret seed, then public key.

    The halves are not a real Ed25519 pair; this module does not check that,
    and says so.
    """
    rng = random.Random(seed)
    secret = rng.randbytes(32)
    return secret + (public if public is not None else rng.randbytes(32))


def public_of(pair: bytes) -> str:
    return base58.encode(pair[32:])


@pytest.fixture
def keydir(tmp_path: Path) -> Path:
    if enclosing_git_tree(tmp_path) is not None:
        pytest.skip("the temp directory sits inside a git tree on this machine")
    return tmp_path


@pytest.fixture
def private_files(monkeypatch):
    """Stand in for chmod 600, which Windows cannot express."""
    monkeypatch.setattr(keys, "_file_mode", lambda path: 0o100600)


def write_key(directory: Path, pair: bytes, name: str = "devnet-validator.json") -> Path:
    path = directory / name
    path.write_text(json.dumps(list(pair)), encoding="utf-8")
    return path


def settings(path: Path, pair: bytes, network: Network = Network.DEVNET) -> KeySettings:
    return KeySettings(path, network, public_of(pair))


class TestLoading:
    def test_a_private_key_outside_any_repository_loads(self, keydir, private_files):
        pair = keypair()
        key = load_validator_key(settings(write_key(keydir, pair), pair), os_name=POSIX)

        assert key.public_key == public_of(pair)
        assert key.network is Network.DEVNET
        assert key.keypair_bytes() == pair

    def test_loading_logs_the_public_key_and_nothing_else(self, keydir, private_files, caplog):
        pair = keypair()
        with caplog.at_level(logging.INFO):
            load_validator_key(settings(write_key(keydir, pair), pair), os_name=POSIX)

        assert public_of(pair) in caplog.text
        assert pair[:32].hex() not in caplog.text
        assert str(list(pair[:8]))[1:-1] not in caplog.text


class TestTheKeyNeverLeaks:
    @pytest.fixture
    def key(self, keydir, private_files):
        pair = keypair()
        return load_validator_key(settings(write_key(keydir, pair), pair), os_name=POSIX)

    def test_repr_str_and_format_show_only_the_public_key(self, key):
        secret = key.keypair_bytes()[:32]
        for shown in (repr(key), str(key), f"{key}", "%s" % key):
            assert key.public_key in shown
            assert secret.hex() not in shown
            assert str(secret[0]) + ", " + str(secret[1]) not in shown

    @pytest.mark.parametrize("duplicate", [pickle.dumps, copy.copy, copy.deepcopy])
    def test_it_cannot_be_pickled_or_copied(self, key, duplicate):
        with pytest.raises(TypeError, match="never pickled"):
            duplicate(key)

    def test_it_has_no_dict_to_dump(self, key):
        """`vars(key)` or a debugger's object dump would otherwise show the bytes."""
        with pytest.raises(TypeError):
            vars(key)


class TestWhereTheFileLives:
    def test_a_relative_path_is_refused(self, private_files):
        pair = keypair()
        with pytest.raises(KeyRefused, match="absolute path"):
            load_validator_key(settings(Path("keys/devnet.json"), pair), os_name=POSIX)

    def test_a_missing_file_is_refused(self, keydir):
        pair = keypair()
        with pytest.raises(KeyRefused, match="no keypair file"):
            load_validator_key(settings(keydir / "absent.json", pair), os_name=POSIX)

    @pytest.mark.parametrize("git_marker", ["directory", "file"])
    def test_a_key_inside_a_git_tree_is_refused(self, keydir, private_files, git_marker):
        """`.git` is a file in a worktree or submodule; both count."""
        repo = keydir / "checkout"
        (repo / "validator" / "secrets").mkdir(parents=True)
        if git_marker == "directory":
            (repo / ".git").mkdir()
        else:
            (repo / ".git").write_text("gitdir: ../elsewhere\n", encoding="utf-8")
        pair = keypair()
        path = write_key(repo / "validator" / "secrets", pair)

        with pytest.raises(KeyRefused, match="inside the git working tree"):
            load_validator_key(settings(path, pair), os_name=POSIX)


class TestFilePermissions:
    @pytest.mark.parametrize("mode", [0o600, 0o400])
    def test_owner_only_is_accepted(self, keydir, monkeypatch, mode):
        monkeypatch.setattr(keys, "_file_mode", lambda path: 0o100000 | mode)
        pair = keypair()

        load_validator_key(settings(write_key(keydir, pair), pair), os_name=POSIX)

    @pytest.mark.parametrize("mode", [0o644, 0o640, 0o604, 0o660, 0o666])
    def test_anything_others_can_touch_is_refused(self, keydir, monkeypatch, mode):
        monkeypatch.setattr(keys, "_file_mode", lambda path: 0o100000 | mode)
        pair = keypair()

        with pytest.raises(KeyRefused, match=f"mode {mode:03o}.*chmod 600"):
            load_validator_key(settings(write_key(keydir, pair), pair), os_name=POSIX)

    def test_windows_may_load_a_localnet_key(self, keydir, caplog):
        pair = keypair()
        with caplog.at_level(logging.WARNING):
            load_validator_key(
                settings(write_key(keydir, pair), pair, Network.LOCALNET), os_name="nt"
            )

        assert "permissions_unchecked" in caplog.text

    @pytest.mark.parametrize("network", [Network.DEVNET, Network.MAINNET])
    def test_windows_refuses_anything_that_holds_value(self, keydir, network):
        pair = keypair()
        with pytest.raises(KeyRefused, match="Run the validator on Linux"):
            load_validator_key(settings(write_key(keydir, pair), pair, network), os_name="nt")


class TestFileContents:
    SECRET_LOOKING = "5Kd3NBUAdUnhyzenEwVLy9pBKxSwXvE9FMPyR4UKZvpe6E3AgLr"

    @pytest.mark.parametrize(
        "content",
        [
            SECRET_LOOKING,
            json.dumps({"secret": SECRET_LOOKING}),
            json.dumps(list(range(63))),
            json.dumps(list(range(65))),
            json.dumps([256] + [0] * 63),
            json.dumps([-1] + [0] * 63),
            json.dumps(["1"] * 64),
            json.dumps([True] * 64),
            json.dumps([1.0] * 64),
        ],
    )
    def test_anything_but_64_bytes_is_refused_without_quoting_it(
        self, keydir, private_files, content
    ):
        path = keydir / "devnet-validator.json"
        path.write_text(content, encoding="utf-8")

        with pytest.raises(KeyRefused, match="not a Solana keypair file") as refused:
            load_validator_key(settings(path, keypair()), os_name=POSIX)

        assert self.SECRET_LOOKING not in str(refused.value)
        # No chained exception either: a JSON error's context can quote the file,
        # and a traceback prints the whole chain.
        error = refused.value
        assert error.__cause__ is None
        assert error.__context__ is None or error.__suppress_context__

    def test_a_large_file_is_not_read_as_a_key(self, keydir, private_files):
        path = keydir / "devnet-validator.json"
        path.write_text(" " * 5000 + json.dumps(list(keypair())), encoding="utf-8")

        with pytest.raises(KeyRefused, match="too large"):
            load_validator_key(settings(path, keypair()), os_name=POSIX)


class TestWhichKey:
    def test_the_wrong_key_for_this_network_is_refused(self, keydir, private_files):
        devnet_key = keypair(seed=1)
        mainnet_run = keypair(seed=2)
        path = write_key(keydir, devnet_key)

        with pytest.raises(KeyRefused, match="Wrong key for this network") as refused:
            load_validator_key(
                KeySettings(path, Network.MAINNET, public_of(mainnet_run)), os_name=POSIX
            )

        assert public_of(devnet_key) in str(refused.value)
        assert public_of(mainnet_run) in str(refused.value)

    @pytest.mark.parametrize("burned", sorted(BURNED_PUBLIC_KEYS))
    def test_a_key_that_was_ever_committed_is_refused(self, keydir, private_files, burned):
        """Even when it is configured as the expected key: git history keeps
        the secret half forever."""
        pair = keypair(public=base58.decode(burned))
        path = write_key(keydir, pair)

        with pytest.raises(KeyRefused, match="committed to the repository"):
            load_validator_key(KeySettings(path, Network.LOCALNET, burned), os_name=POSIX)


class TestSettingsFromEnvironment:
    def test_reads_all_three(self):
        pair = keypair()
        env = {
            ENV_KEYPAIR_PATH: "/home/validator/.config/s2e/devnet.json",
            ENV_NETWORK: "devnet",
            ENV_PUBLIC_KEY: public_of(pair),
        }

        loaded = KeySettings.from_env(env)

        assert loaded.path == Path("/home/validator/.config/s2e/devnet.json")
        assert loaded.network is Network.DEVNET

    def test_names_every_missing_variable(self):
        with pytest.raises(KeyRefused, match=f"{ENV_KEYPAIR_PATH}, {ENV_NETWORK}, {ENV_PUBLIC_KEY}"):
            KeySettings.from_env({})

    def test_an_unknown_network_is_refused(self):
        env = {ENV_KEYPAIR_PATH: "/k.json", ENV_NETWORK: "testnet", ENV_PUBLIC_KEY: public_of(keypair())}

        with pytest.raises(KeyRefused, match="localnet, devnet, mainnet"):
            KeySettings.from_env(env)

    @pytest.mark.parametrize("bad", ["not-base58-0OIl", "abc", base58.encode(bytes(33))])
    def test_the_expected_key_must_be_a_public_key(self, bad):
        with pytest.raises(KeyRefused, match="must be a Solana public key"):
            KeySettings(Path("/k.json"), Network.DEVNET, bad)


class TestBase58:
    def test_the_system_program_address(self):
        """32 zero bytes: every leading zero is a leading '1'."""
        assert base58.encode(bytes(32)) == "1" * 32
        assert base58.decode("1" * 32) == bytes(32)

    @pytest.mark.parametrize("seed", range(20))
    def test_round_trip(self, seed):
        data = random.Random(seed).randbytes(32)

        assert base58.decode(base58.encode(data)) == data

    def test_a_known_vector(self):
        assert base58.encode(b"hello world") == "StV1DL6CwTryKyV"

    @pytest.mark.parametrize("char", ["0", "O", "I", "l", " "])
    def test_characters_outside_the_alphabet_are_refused(self, char):
        with pytest.raises(ValueError, match="not base58"):
            base58.decode(f"abc{char}")


def test_the_burned_keys_really_are_32_byte_public_keys():
    for burned in BURNED_PUBLIC_KEYS:
        assert len(base58.decode(burned)) == 32


class TestKeypairFiles:
    def test_a_created_file_reads_back_and_is_never_overwritten(self, tmp_path):
        from s2e_validator.settlement import create_keypair_file, read_keypair_file

        path = tmp_path / "new.json"
        create_keypair_file(path, bytes(range(64)))

        assert read_keypair_file(path) == bytes(range(64))
        with pytest.raises(FileExistsError):
            create_keypair_file(path, bytes(64))
        assert read_keypair_file(path) == bytes(range(64))

    def test_only_a_whole_keypair_is_written(self, tmp_path):
        from s2e_validator.settlement import create_keypair_file

        with pytest.raises(ValueError, match="64 bytes"):
            create_keypair_file(tmp_path / "short.json", bytes(32))
        assert not (tmp_path / "short.json").exists()
