"""Making the validator's signing key on the server."""

from __future__ import annotations

import json

import pytest

pytest.importorskip("solders", reason="the key tool needs the optional `solana` extra")

from solders.keypair import Keypair  # noqa: E402

from s2e_validator.tools import new_key  # noqa: E402


def test_it_prints_the_public_key_and_nothing_secret(tmp_path, capsys):
    path = tmp_path / "validator.json"

    assert new_key.main(["--out", str(path)]) == 0

    printed = capsys.readouterr()
    keypair = Keypair.from_bytes(bytes(json.loads(path.read_text())))
    assert printed.out.strip() == str(keypair.pubkey())
    assert printed.err == ""


def test_an_existing_key_is_never_overwritten(tmp_path, capsys):
    path = tmp_path / "validator.json"
    path.write_text("keep me")

    with pytest.raises(SystemExit):
        new_key.main(["--out", str(path)])

    assert path.read_text() == "keep me"
    assert "never overwritten" in capsys.readouterr().err


def test_a_key_is_never_made_inside_a_repository(tmp_path, capsys):
    (tmp_path / ".git").mkdir()

    with pytest.raises(SystemExit):
        new_key.main(["--out", str(tmp_path / "validator.json")])

    assert not (tmp_path / "validator.json").exists()
    assert "inside the git working tree" in capsys.readouterr().err


def test_the_path_must_be_absolute(capsys):
    with pytest.raises(SystemExit):
        new_key.main(["--out", "validator.json"])

    assert "absolute path" in capsys.readouterr().err
